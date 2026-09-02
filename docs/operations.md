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

**With `STORAGE_BACKEND=s3` (ADR-0011, optional — see §13):** PostgreSQL
remains authoritative exactly as above. `/data/sources` remains mandatory in
both modes (it is never optional or disposable once S3 is enabled — see
§13.2), but a finalized Source original's *bytes* live in the configured S3
bucket/prefix instead of under `/data/sources`. The configured S3 namespace
therefore joins PostgreSQL and `/data/sources` as data that must be protected
according to the operator's own durability/backup policy — see §13.10.

## 2. First start (empty database)

Principle, unchanged from the architecture: **migration is explicit, never
automatic at application startup.** `/health/ready` detects a schema that
does not match the application's expected revision and reports `not_ready`
rather than guessing or auto-applying anything.

This section describes the default `STORAGE_BACKEND=filesystem` path, which
is entirely unaffected by ADR-0011. If you are enabling
`STORAGE_BACKEND=s3` on a **fresh** install, this same procedure applies
unchanged (a fresh S3 install has no legacy Sources to converge, so startup
reaches ready quickly); enabling S3 on an **existing** filesystem
installation instead follows §13.7's dedicated upgrade procedure.

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
  application starts, and never on every deploy as a matter of course.
- A schema that doesn't match the application's expected revision leaves
  `/health/ready` at `not_ready`; the application does not guess or
  self-heal.
- A deployment is not healthy until migration has completed (when required)
  and readiness is confirmed.

**When `alembic upgrade head` is required, precisely:**

| Situation | `alembic upgrade head` required? |
|---|---|
| Fresh installation — brand-new, empty PostgreSQL database/volume (§2) | **Yes.** |
| Ordinary application redeploy/restart (no volume change, no version change) | No. |
| Recreating a Compose Stack/service (Portainer, EasyPanel, or otherwise) while reusing the same, already-migrated PostgreSQL persistent volume | No — recreating the Stack does not reset or otherwise affect the schema already present in that volume. |
| Upgrading to a release with **no** new database migration | No — proceed straight to the normal health/readiness/smoke verification (§I); there is nothing to apply. |
| Upgrading to a release **with** one or more new migrations | **Yes**, once, using the target release's image — follow the full backup/quiesce upgrade procedure (§4). |
| A new Stack pointed at a NEW/EMPTY PostgreSQL volume or database (even if it isn't literally "the first" Stack you've ever created) | **Yes** — this is a fresh installation by definition, regardless of how many prior Stacks exist elsewhere. |

Re-running `alembic upgrade head` when already at the target head is normally
idempotent (Alembic detects there is nothing left to apply and exits
cleanly) — the point of the table above is not that repeating it is
dangerous, but that it is not an operational requirement you need to perform
on every deploy, restart, or Stack recreation.

`alembic current` and `alembic heads` are **read-only, diagnostic/
verification commands** — they never mutate the schema. They are useful to
confirm what revision is actually applied (before a backup, after a
migration, after a restore, or just to check), but running them is never
itself a required deployment step; they answer "what state is this database
in?", they don't change it. Both run the same way as the migration command
above, substituting the subcommand:

```bash
docker compose run --rm sofias-memory alembic current
docker compose run --rm sofias-memory alembic heads
```

## 4. Upgrade

**First, check whether the target release adds any migration at all** — read
its `CHANGELOG.md` entry and/or check `migrations/versions/` for anything
newer than the currently-deployed revision (`alembic heads` against the
target image vs. `alembic current` against the running deployment, §3).
This determines whether step 5 below is performed or skipped.

1. Read the release notes for the target version.
2. Take a backup (§6) at the currently-deployed version.
3. Obtain the target image — either a published GHCR tag/digest (§C; the
   normal path today) or, for a local/source-build deployment, pull/checkout
   the target version's source and rebuild (`docker compose build
   sofias-memory`).
4. Quiesce the application (`docker compose stop sofias-memory`) so no new
   writes land during the migration window.
5. **If, and only if, the target release adds a new migration:** run it
   using the target image: `docker compose run --rm sofias-memory alembic
   upgrade head`. **If the target release adds no new migration, skip this
   step entirely** — there is nothing to apply, and running it anyway is
   harmless (idempotent at head) but not required.
6. Start the application on the target image: `docker compose up -d
   sofias-memory`.
7. Verify readiness (`/health/ready`).
8. Run a smoke check against a non-production dataset before considering the
   upgrade complete.

Both the published-GHCR-image path and the local-build path follow the same
eight steps above — only how the target image is obtained (step 3) differs;
see §C for the published-image details.

Sofias Memory does **not** promise zero-downtime upgrades. The model above requires
a maintenance window (the app is quiesced for the duration of the migration).
For a single-user MVP, this trade-off is intentional: it favors a simple,
recoverable procedure over rolling-migration complexity that nothing in the
current architecture is designed to support.

Steps 4 and 6 above (stop the old container, then start the new/target one)
already give every deployment path in this document **stop-old-before-
start-new** process exclusivity by construction — this document has never
recommended starting a replacement container before stopping the one it
replaces. This is a hard requirement, not merely today's convenience, for
any `STORAGE_BACKEND=s3` deployment (ADR-0011 D43) — see §13.14 for why, and
for the one platform (Easypanel) where this document cannot independently
verify the redeploy mechanics and says so explicitly.

Enabling `STORAGE_BACKEND=s3` for the first time on an existing installation
is its own dedicated procedure, not an ordinary upgrade — see §13.7.

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

**Rolling back `STORAGE_BACKEND` itself (`s3` → `filesystem`) is a distinct
concern from application/schema rollback above — see §13.12/§13.13 before
attempting either.** In short: switching the setting back is supported only
as a *write-backend* switch on the same, still-S3-capable application
version (new writes go to filesystem again; existing `s3://` Sources are
untouched and still need working S3 configuration to be read or deleted).
Rolling back to an **older application release that predates S3 support** is
a different, higher-risk operation once any Source has actually been
migrated to `s3://` — §13.13 covers the required backup-based recovery path.

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

**With `STORAGE_BACKEND=s3` (ADR-0011):** the procedure above is unchanged —
PostgreSQL and `/data/sources` are still backed up exactly as described (the
sources volume still holds durable ingress and any not-yet-migrated legacy
originals, §13.2). In addition, back up the configured S3 bucket/prefix
according to your provider's own snapshot/versioning/replication policy —
this document does not prescribe an S3 backup mechanism, since that is
provider-specific (AWS S3 versioning + cross-region replication, MinIO's own
mirroring, etc.). See §13.10 for the full per-mode backup contract and
§13.11 for restore-consistency considerations specific to a filesystem→S3
transition.

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

**With `STORAGE_BACKEND=s3` (ADR-0011):** restoring PostgreSQL to an earlier
point while the configured S3 bucket still contains objects written *after*
that point is safe but can leave redundant, no-longer-referenced
deterministic objects in S3 (PostgreSQL no longer points at them) — this is
strictly safer than the reverse (PostgreSQL pointing at S3 objects that no
longer exist), so it is the accepted default outcome, not an error to
correct. **Do not** "clean up" by deleting a broad S3 prefix as part of a
normal restore — every deletion this application ever performs targets one
exact, deterministic key (ADR-0011 D6/D24/D36); a manual prefix-wide delete
is outside that discipline and can destroy objects still referenced by rows
the restore just brought back. If reconciliation of orphaned S3 objects is
ever needed, do it as a separate, deliberate, exact-key-audited operation —
never as a step of this restore procedure.

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
architecture: `compose.yaml` is still the canonical, portable stack
definition. Portainer consumes it directly today (§F) — there is no
dedicated Portainer variant. **EasyPanel deployments use a dedicated
variant**, [`deploy/easypanel/compose.yaml`](../deploy/easypanel/compose.yaml)
(§G; see [`docs/deployment/easypanel.md`](deployment/easypanel.md) for the
full, verified guide) — everything below in §A-§E still applies to it
identically (same image, same migration policy, same security posture), only
the Compose file and the platform-specific deploy mechanics differ.

### A. Deployment paths

There are exactly two supported ways to get the application running onto
`compose.yaml` (or its EasyPanel variant, §G):

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
ghcr.io/kallbuloso/sofias-memory:0.1.2
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

- Prefer an exact version tag: `ghcr.io/kallbuloso/sofias-memory:0.1.2`.
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

EasyPanel uses a dedicated Compose variant,
[`deploy/easypanel/compose.yaml`](../deploy/easypanel/compose.yaml) — the
same three-service topology, database images, security posture, and
environment contract as the root `compose.yaml`, differing only in
`image:` (no build context) and no host port published for `sofias-memory`
(EasyPanel routes its domain to the internal port directly instead).

The full, real-deployment-verified procedure — creating the service, both
valid source options (Git or Inline), required environment variables, the
first-install migration steps via EasyPanel's browser console, health
verification, domain configuration, and production smoke — is documented in
[`docs/deployment/easypanel.md`](deployment/easypanel.md). It follows the
same migration policy as §3/§4 above: required once on a fresh install or on
an upgrade that adds new migrations, never merely because the Stack was
redeployed or recreated with its existing PostgreSQL volume intact.

**If you use `STORAGE_BACKEND=s3` (ADR-0011): read §13.14 before your next
redeploy.** This document's verified deployment evidence (§ "Validation
evidence" in `docs/deployment/easypanel.md`) covers first install and normal
application behavior — it does **not** include an independent verification
of exactly how EasyPanel sequences container replacement during a redeploy
(whether it always fully stops the running container before starting its
replacement, or can briefly run both). §13.14 states this gap explicitly and
gives the safe, manual alternative (stop the service yourself before
redeploying) to use until that platform behavior is independently confirmed.

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
2. `/health/ready` returns `200` with all checks ready (`postgres`, `neo4j`,
   `worker`, plus `process_state` when `STORAGE_BACKEND=s3` is configured —
   see §13.1) `ready`.
3. `GET /api/v1/info` reports the version you expect to have deployed.
4. `scripts/production_smoke.py` (§H) exits `0`.
5. For an upgrade specifically: `alembic current` matches the target
   version's expected head (§3), and the release notes for that version have
   been read.

If any of these fail, treat the deployment as not done — do not route
production traffic to it — and consult §10 (Failure / abort conditions) for
what to check before retrying.

## 13. S3-compatible Source storage (ADR-0011, optional)

Everything in §§1–12 describes the default `STORAGE_BACKEND=filesystem`
behavior, which ADR-0011 leaves entirely unchanged. This section documents
the **optional** `STORAGE_BACKEND=s3` backend end to end: configuration,
IAM, the upgrade/rollback procedures, backup/restore implications, and
troubleshooting. It is written directly against this repository's actual
implemented `Settings` fields and S3 adapter
(`sofias_memory/config.py`, `sofias_memory/infrastructure/storage/s3.py`) and
the accepted contract in `docs/adr/0011-durable-source-object-storage-s3-and-startup-convergence.md`
— cite that ADR's `D`-section ids for the full rationale behind any rule
stated here.

**Scope note on how this section was produced.** Every claim below was
checked against the actual runtime code and its own test suite (unit and
integration), the way the rest of this document's procedures were checked
against a real deployment. Unlike §§1–12, however, the **end-to-end S3/MinIO
walkthrough itself was not executed** while writing this section — no Docker
runtime was available in the environment this section was authored in. This
is a real, disclosed limitation, not a claim of equivalent verification;
treat the procedures below as reviewed-correct-against-the-implementation,
not as separately walked-through-live evidence the way §2–§12 already are.
An operator following this section for the first time should treat it with
the same care as any first production use of a new capability.

### 13.1. Configuration surface

Exact `Settings` fields implemented (`sofias_memory/config.py`) — do not
configure anything not listed here:

| Variable | Required? | Default | Notes |
|---|---|---|---|
| `STORAGE_BACKEND` | No | `filesystem` | `filesystem` or `s3`. Controls only where **new** finalized Source originals are written (D2). |
| `STORAGE_S3_BUCKET` | **Yes, if `s3`** | — | Bucket name. |
| `STORAGE_S3_PREFIX` | No | `""` (bucket root) | Application-managed namespace prefix (§13.5/D36) — normalized, no leading/trailing `/`, no `.`/`..`/empty segments. |
| `STORAGE_S3_REGION` | **Yes, if `s3`** | — | AWS region (or the region value your S3-compatible provider expects). |
| `STORAGE_S3_ENDPOINT_URL` | No | unset (real AWS S3) | Absolute `http(s)://` URL — only for an S3-compatible endpoint (MinIO, etc.) or non-default AWS partition. |
| `STORAGE_S3_ACCESS_KEY_ID` | No | unset | Static credential. Must be set together with `STORAGE_S3_SECRET_ACCESS_KEY` or not at all. |
| `STORAGE_S3_SECRET_ACCESS_KEY` | No | unset | Static credential (secret). |
| `STORAGE_S3_SESSION_TOKEN` | No | unset | Only valid together with the access key/secret pair above. |
| `STORAGE_S3_MAX_CONCURRENCY` | No | `4` | Bounds concurrent S3 operations (same pattern as `LLM_MAX_CONCURRENCY`). |

**Credentials are optional even under `STORAGE_BACKEND=s3`** (§13.3) —
leaving `STORAGE_S3_ACCESS_KEY_ID`/`STORAGE_S3_SECRET_ACCESS_KEY` unset is a
valid, supported configuration when the standard AWS credential provider
chain resolves credentials another way (container/instance role, shared
config file, environment). `STORAGE_S3_ENDPOINT_URL` accepts only an
absolute `http(s)://` URL — there is **no** path-style-addressing toggle,
TLS-disable flag, or custom-CA setting in this implementation; if your
S3-compatible provider requires one of those to work, that is a genuine gap
against this implementation, not a missing configuration value — do not
attempt to fake it with an unsupported field.

Every one of these values follows the same rules as every other `Settings`
field (§10 of `AGENTS.md`): loaded once at startup, validated exhaustively,
immutable during the process's lifetime, and never logged, persisted,
returned to a client, or included in `/api/v1/info`'s config fingerprint
(ADR-0011 D18).

**Backend behavior, exactly:**

- `STORAGE_BACKEND=filesystem` (default) — new Source originals are written
  under `DATA_DIRECTORY`, exactly as before ADR-0011. No S3 configuration is
  read or required. No startup convergence gate exists for this backend at
  all — the process reaches `OPERATIONAL` exactly as it always has.
- `STORAGE_BACKEND=s3` — new Source originals are written to the configured
  bucket/prefix, **and** startup runs a one-time-per-boot storage
  convergence pass that migrates any existing `file://` Sources to S3
  automatically (§13.7) before the process becomes ready for normal traffic.
- **Reads and deletes of an existing Source always follow that Source's own
  `storage_uri` scheme** (`file://` or `s3://`), never `STORAGE_BACKEND` —
  a dataset with a mix of both is expected and fully supported mid-migration
  (D5).
- There is **no** automatic `s3://` → `filesystem` reverse migration, and
  **no** automatic relocation between S3 buckets/prefixes (D25). Setting
  `STORAGE_BACKEND=filesystem` on an installation that already has `s3://`
  Sources does not download them back — see §13.9/§13.12.

### 13.2. `DATA_DIRECTORY` remains mandatory in BOTH modes

**Do not remove the `/data/sources` persistent volume, and do not treat it
as optional, merely because finalized Source originals may live in S3.**
`DATA_DIRECTORY` remains mandatory, persistent application state under
`STORAGE_BACKEND=s3` because it owns, at minimum (ADR-0011 D1/D22):

- durable Remember `_ingress/` staging (crash-recoverable, backend-agnostic,
  D3) — a queued Remember run's staged bytes survive a `filesystem → s3`
  configuration flip and redeploy unaffected;
- the legacy local copy of any Source still awaiting filesystem→S3
  migration, and the brief window where a just-migrated Source's local
  duplicate awaits confirmed post-repoint cleanup (D9);
- protected/unrecognized content that must never be recursively scanned or
  destroyed by any storage-cleanup logic (D24).

`compose.yaml` and `deploy/easypanel/compose.yaml` both keep the
`sofias_memory_sources` volume mounted at `/data/sources` unconditionally —
this slice added `STORAGE_BACKEND`/`STORAGE_S3_*` environment forwarding to
both files and changed nothing about the volume declaration itself.

### 13.3. Credentials — explicit or provider chain

Two supported, equally valid ways to authenticate to S3 under
`STORAGE_BACKEND=s3`:

1. **Explicit static credentials** — set `STORAGE_S3_ACCESS_KEY_ID` and
   `STORAGE_S3_SECRET_ACCESS_KEY` (and `STORAGE_S3_SESSION_TOKEN` if your
   provider issues temporary credentials that need one). Both of the first
   two must be set together — setting only one is a startup configuration
   error.
2. **Provider credential chain** — leave all three unset. The adapter falls
   back to the standard AWS credential provider chain (environment
   variables outside `Settings`, a shared credentials file, or a
   container/instance role such as an IAM role for an ECS task or EC2
   instance profile) exactly as `boto3`'s default client behavior already
   does. This is the right choice when your deployment platform already
   provides workload identity/instance-role credentials — no static secret
   needs to exist anywhere in your environment configuration at all.

Never commit real credentials to `.env`, `compose.yaml`,
`deploy/easypanel/compose.yaml`, or any file in this repository — the
example files below use empty/placeholder values only (§13.4).

### 13.4. MinIO / S3-compatible configuration example

```env
STORAGE_BACKEND=s3
STORAGE_S3_BUCKET=sofias-memory-sources
STORAGE_S3_PREFIX=production
STORAGE_S3_REGION=us-east-1
STORAGE_S3_ENDPOINT_URL=https://minio.internal.example:9000
STORAGE_S3_ACCESS_KEY_ID=REPLACE_WITH_YOUR_ACCESS_KEY
STORAGE_S3_SECRET_ACCESS_KEY=REPLACE_WITH_YOUR_SECRET_KEY
STORAGE_S3_MAX_CONCURRENCY=4
```

For real AWS S3, omit `STORAGE_S3_ENDPOINT_URL` entirely and set
`STORAGE_S3_REGION` to the bucket's actual region; credentials may be
omitted in favor of an IAM role (§13.3). `STORAGE_S3_REGION` is still
required by this implementation even when using an S3-compatible endpoint
that does not itself have "regions" in the AWS sense — most such providers,
including MinIO, accept an arbitrary placeholder value here (e.g.
`us-east-1`); consult your provider's own `boto3`-compatibility notes if
unsure.

A local MinIO container for development/testing (not part of production
`compose.yaml` — add it only to a local/dev override file if you need one,
never to the production stack):

```yaml
minio:
  image: minio/minio:latest
  command: server /data --console-address ":9001"
  environment:
    MINIO_ROOT_USER: minioadmin
    MINIO_ROOT_PASSWORD: minioadmin-change-me
  ports:
    - "9000:9000"
    - "9001:9001"
  volumes:
    - minio_data:/data
```

then create the bucket (via the MinIO console at `:9001`, or `mc mb`) before
starting Sofias Memory against it.

### 13.5. S3 prefix ownership / IAM permissions

**The configured `STORAGE_S3_PREFIX` namespace is application-managed and
Sofias Memory must be its exclusive writer** (ADR-0011 D36) — no other
application or process may create, overwrite, version, or delete objects
inside `s3://<bucket>/<STORAGE_S3_PREFIX>/v1/sources/...` (or the
`v1/system/probe/...` prefix the startup probe uses). This is a
**prefix-scoped**, not bucket-wide, requirement — the same bucket may host
other applications under a different prefix. Read-only backup/replication/
audit tooling against the managed prefix is fine; anything that writes to
it is not.

The actual `boto3` operations this implementation performs (confirmed by
reading `sofias_memory/infrastructure/storage/s3.py`, not assumed from the
ADR alone): `put_object`, `get_object`, `head_object`, `delete_object`,
`delete_objects`, `get_bucket_versioning`, and `list_object_versions`
(paginated). The minimum IAM actions those calls require, scoped as tightly
as each action allows:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "SofiasMemoryManagedPrefixObjectAccess",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:DeleteObjectVersion",
        "s3:GetObjectVersion"
      ],
      "Resource": "arn:aws:s3:::REPLACE_WITH_BUCKET/REPLACE_WITH_PREFIX/*"
    },
    {
      "Sid": "SofiasMemoryBucketLevelListAndVersioning",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:ListBucketVersions",
        "s3:GetBucketVersioning"
      ],
      "Resource": "arn:aws:s3:::REPLACE_WITH_BUCKET",
      "Condition": {
        "StringLike": {
          "s3:prefix": "REPLACE_WITH_PREFIX/*"
        }
      }
    }
  ]
}
```

Notes:

- `s3:GetObject`/`s3:PutObject` also cover this implementation's
  `head_object` calls (S3's HEAD operation is authorized under the same
  `s3:GetObject` action, not a separate IAM action).
- `s3:DeleteObjectVersion`/`s3:GetObjectVersion` are only exercised on a
  versioned bucket (§13.6); harmless to grant on an unversioned one.
- `s3:ListBucket`/`s3:ListBucketVersions` are bucket-level actions in AWS
  IAM — they cannot be scoped to an object-key ARN, which is why they are
  granted at the bucket level above, narrowed instead by the `s3:prefix`
  condition to the managed prefix's own key space.
- **Do not grant `s3:*`** as a shortcut — it grants bucket administration,
  policy, and lifecycle-management capabilities this application never
  uses and never needs.
- S3-compatible providers (MinIO, etc.) generally implement an equivalent
  permission model but may use different policy syntax/action names —
  consult your provider's own IAM/policy documentation; the *capabilities*
  above (get/put/delete object, delete/get object version, list bucket,
  list bucket versions, get bucket versioning) are what must be granted
  regardless of the exact policy language.

### 13.6. Versioning, Object Lock, and `UNRESOLVED` cleanup debt

This implementation supports all three bucket versioning states:

- **Unversioned** — delete removes the object; a subsequent `HEAD` confirms
  absence before the adapter reports success.
- **Versioning Enabled** — delete enumerates every version and delete
  marker for the exact key, deletes them all, then re-lists to confirm
  nothing remains before reporting success. A delete marker alone (the
  "current" version hidden, older versions still retrievable) is **not**
  treated as deleted.
- **Versioning Suspended** — handled the same way as Enabled (both report a
  non-empty `get_bucket_versioning` status); the same exact-key
  list/delete/verify sequence applies.

**When physical deletion cannot be proven** — permissions, Object Lock,
legal hold/retention, a network/provider failure, or verification itself
failing — the adapter reports a typed `UNRESOLVED` outcome rather than
guessing. Under ADR-0011 (D37), **this does not block Sofias Memory's own
authoritative deletion**: a full Forget / Dataset Forget / Forget Everything
/ administrative Dataset DELETE still completes, the Source reaches
`DELETED` in PostgreSQL, and the run itself still succeeds — with the
physical cleanup recorded as unresolved in the run's own metrics
(`storage_unresolved`). **`UNRESOLVED` is never physical deletion success**
— the Source's `storage_uri` is deliberately preserved (not cleared) on the
`DELETED` tombstone specifically so the retained locator documents exactly
what physical cleanup remains outstanding. If your compliance posture
requires guaranteed physical purge (e.g. Object Lock intentionally
prevents it until a retention period expires), track `storage_unresolved`
in your own monitoring and treat a nonzero value as an operational
follow-up item, not a Sofias-Memory-level failure.

### 13.7. Upgrade procedure: filesystem → S3

Enabling `STORAGE_BACKEND=s3` on an **existing** filesystem installation
that already has Sources:

1. Confirm your deployment already follows this document's stop-old-before-
   start-new deployment discipline (§13.14) — this is a hard prerequisite,
   not optional, for this procedure.
2. Take a full pre-transition backup (§6/§13.10) — PostgreSQL and
   `/data/sources`. This is your recovery path if you ever need to roll back
   to a pre-S3-capable release after migration begins (§13.13).
3. Provision the S3 bucket/prefix and IAM credentials (§13.4/§13.5).
4. Confirm the `DATA_DIRECTORY` persistent volume remains mounted — do not
   remove it (§13.2).
5. Set `STORAGE_BACKEND=s3` and the required `STORAGE_S3_*` values in your
   environment configuration.
6. Stop the currently-running (`OPERATIONAL`) application container —
   `docker compose stop sofias-memory` or the equivalent for your platform.
7. Start the new, S3-capable application container with the updated
   configuration — `docker compose up -d sofias-memory` or the equivalent.
8. Observe the process's own state through this boot: `/health/live` reports
   healthy immediately; `/health/ready` reports `not_ready` while the
   process is in `BOOTSTRAP_MAINTENANCE` (schema check) and then
   `STORAGE_CONVERGING` (S3 probe + migration); watch the application logs
   for `process_state_transition` and `storage_convergence_*` events
   (§13.15) to see real progress, not merely "still not ready."
9. Allow automatic convergence to finish. **Do not** manually migrate
   Sources, rewrite `storage_uri` values, or copy files into S3 yourself —
   the startup convergence gate does this automatically and idempotently;
   manual intervention is never required for the normal path and risks
   conflicting with it.
10. Verify: `/health/ready` reports `ready`, with a `process_state` check
    reporting ready (in addition to `postgres`/`neo4j`/`worker`).
11. Only once step 10 passes, resume/declare normal service operation and
    run a production smoke check (§H) as usual.

**How long does convergence take?** Proportional to the number of legacy
`file://` Sources and, in the rare crash/interrupted-deletion case, to any
in-flight Forget/Dataset-DELETE lineage that must reach its own terminal
state first. There is no fixed timeout — a large existing install should
expect a real, possibly long, one-time delay on this first S3 boot (ADR-0011
D33); this is deliberate fail-closed behavior, not a hang, and is why the
container healthcheck targets `/health/live`, never `/health/ready`
(§13.14).

### 13.8. Startup S3 outage

If S3 is unreachable (bad credentials, network problem, wrong bucket/
endpoint, IAM denial) when the process boots under `STORAGE_BACKEND=s3`,
**the process is not dead** — it stays alive, logs the failure, and retries:

- `/health/live` continues reporting healthy.
- `/health/ready` stays `not_ready`.
- The business API continues returning a maintenance/dependency-unavailable
  response (`503`) for every route except the health endpoints.
- The process remains in `STORAGE_CONVERGING`, retrying the bootstrap
  sequence on a short fixed interval, until the underlying condition
  resolves.

**Do not** repeatedly restart/recreate the container solely because
readiness is `false` shortly after start — that resets nothing useful and
simply restarts the same retry loop. Instead, fix the actual condition
(credentials, IAM policy, bucket name, endpoint reachability/DNS/network
path) and let the already-running process's own retry loop pick it up; use
the log events in §13.15 to identify which condition is actually failing.

### 13.9. Filesystem mode after S3 history

Setting `STORAGE_BACKEND=filesystem` on an installation that has `s3://`
Sources does **not** scan for or require S3 configuration merely to reach
`OPERATIONAL` — the filesystem backend never inspects historical `s3://`
rows at startup, and there is no S3-related readiness dimension for this
backend at all. Existing `s3://` Sources remain durable, valid locators;
they are simply not proactively touched. If a later operation actually
needs one of them (a Cognify rehydration read, or a Forget/Dataset-DELETE
that targets it), that operation still routes by the Source's own
`storage_uri` scheme (D5) and will need a working S3 configuration at that
moment — if S3 configuration is genuinely absent for a delete, the runtime
contract in §13.6 applies (`UNRESOLVED`, business deletion still succeeds).
**Historical S3 objects are never automatically moved back to the
filesystem** — there is no reverse migration (D25).

### 13.10. Backup contract

| Mode | Must be backed up |
|---|---|
| `filesystem` | PostgreSQL; `/data/sources` (unchanged from §1/§6). |
| `s3` | PostgreSQL; `/data/sources` (still required — durable ingress, in-transit migration state, §13.2); the configured S3 bucket/prefix, per your provider's own durability/backup policy (this document does not prescribe an S3-specific backup mechanism). |

Neo4j is never authoritative and is never part of the required backup in
either mode (§1/§8). An S3 `ETag` is **not** an integrity hash Sofias Memory
trusts or backs up against — this application's own SHA-256 content
identity (recorded in PostgreSQL) is the only integrity authority it relies
on; do not substitute `ETag` comparisons for a real backup/restore
verification of your S3 data.

**During a filesystem→S3 transition specifically**, a consistent rollback
strategy must account for three things together, not PostgreSQL alone:
PostgreSQL, `/data/sources`, and whatever S3 state has been created since
the transition started (§13.13).

### 13.11. Restore consistency

Restoring PostgreSQL to an earlier point while newer S3 objects still exist
is safe (redundant deterministic objects, never a dangling reference) —
see the restore-section addendum above (§7) for the exact reasoning and the
explicit warning against broad S3-prefix deletion as a "cleanup" step.

### 13.12. Rollback A — switching `STORAGE_BACKEND` back to `filesystem` (same application version)

Fully supported, as a **write-backend switch only**, on the same
S3-capable application version:

- new Source originals go to `DATA_DIRECTORY` again;
- existing `s3://` Sources remain `s3://` — they are not downloaded back
  (§13.9);
- reads/deletes continue to route by each Source's own `storage_uri` scheme
  (D5);
- a working S3 configuration may still be required whenever one of those
  historical `s3://` Sources is actually read or deleted — losing/removing
  S3 configuration entirely while such Sources still exist leads to the
  `UNRESOLVED` behavior in §13.6/§13.8, not a hang or an error.

There is no reverse `s3://` → `file://` migration (D25) — this switch never
moves existing bytes.

### 13.13. Rollback B — reverting to a release older than S3 support

**This is a fundamentally different, higher-risk operation than §13.12
once any Source has actually been migrated/finalized to `s3://`.** An older
application release that predates ADR-0011 does not understand an
`s3://...` `storage_uri` value at all — it was written assuming every
`storage_uri` is a `file://` path.

**Before ever enabling S3 convergence, take a consistent pre-transition
backup (§13.7 step 2 / §13.10).** If you need to roll back to a
pre-S3-capable release after migration has begun, the supported recovery
path is: **restore PostgreSQL and `/data/sources` together from that
pre-transition backup** (§7), then deploy the older release against the
restored state. **Sofias Memory does not promise, and this document does
not describe, any automatic downgrade path** — merely changing
`STORAGE_BACKEND` back and deploying an older binary against a database that
already contains `s3://` rows is not a supported operation.

### 13.14. Process exclusivity — `STORAGE_BACKEND=s3` requires stop-old-before-start-new

**This is a hard deployment requirement for any `STORAGE_BACKEND=s3`
deployment (ADR-0011 D43), not a suggestion.**

**Why.** While the new process is `STORAGE_CONVERGING`, it must be the only
process capable of transitioning a Source to `DELETING` or migrating a
Source to S3. If an old process is still `OPERATIONAL` — still holding
normal worker claims, still able to service a Forget/Dataset-DELETE
request — at the same time a new process is migrating the same Source to
S3, an already-uploaded S3 object can become permanently orphaned (the exact
race ADR-0011's D43 amendment exists to close). `replicas = 1` alone does
**not** prevent this: an orchestrator configured for start-first ("rolling")
deployment overlap will, by design, start the new process before stopping
the old one, creating exactly this unsupported overlap even under a
single-replica setting. Excluding the new process from load
(readiness/load-balancer exclusion) is also insufficient by itself, because
it does not stop the *old* process from continuing to claim work.

**Required deployment model:** stop the old process fully — no longer
serving business routes, no longer claiming work — **before** starting the
new one. This document's own procedures already follow this discipline by
construction:

- `docker compose` / Portainer (§2, §4, §12.F): `docker compose stop
  sofias-memory` always precedes `docker compose up -d sofias-memory` in
  every upgrade procedure in this document. A plain `docker compose up -d`
  recreate of a single already-running service is itself sequential (stop
  old, then start new) — this repository does not use Docker Swarm/`docker
  stack deploy` anywhere, so no rolling-update orchestrator sits between the
  operator and the container here. No additional Compose configuration is
  needed to get this property under the deployment model this repository
  actually uses.
- **Easypanel (§12.G): not independently verified.** This document cannot
  confirm, from inside this repository, exactly how Easypanel's own
  redeploy mechanism sequences container replacement — whether it always
  fully stops the running container before starting its replacement, or can
  briefly run old and new together. **Until that platform behavior is
  independently confirmed for your Easypanel version, manually stop the
  `sofias-memory` service in the Easypanel UI before triggering a redeploy
  whenever `STORAGE_BACKEND=s3` is in use**, rather than relying on
  Easypanel's default redeploy action to sequence this safely on its own.
  This is a deliberately conservative, manual workaround for a genuine gap
  in this document's own verification, not a claim that Easypanel is known
  to behave unsafely.

Multi-replica or true rolling-overlap deployment of `STORAGE_BACKEND=s3` is
explicitly **not** supported by the current architecture (D43) — this is a
single-process MVP constraint, not a temporary limitation this document
works around with configuration.

### 13.15. Observability / troubleshooting

Structured log events this implementation actually emits (confirmed by
reading `sofias_memory/lifespan.py`/`sofias_memory/services/process_state.py`
— never source content, credentials, or `STORAGE_S3_ENDPOINT_URL`/access-key
values):

| Event | Meaning |
|---|---|
| `process_state_transition` | The process moved to `bootstrap_maintenance`, `storage_converging`, or `operational`. The single most useful line to grep for "what state is this process in right now." |
| `bootstrap_attempt_failed` | One full bootstrap attempt failed (schema not current, S3 unreachable, an integrity condition, or a genuine defect) and will retry after a short fixed delay. Includes `exception_type` — a `TypeError`/`ValueError`-shaped defect looks different from a recognized dependency-unavailable condition; treat an unfamiliar `exception_type` as worth investigating even though the process itself stays up. |
| `bootstrap_schema_not_ready` | Schema is not at the expected Alembic head — run `alembic upgrade head` explicitly (§3); this application never does so automatically. |
| `storage_convergence_integrity_failures` | At least one Source failed migration validation (missing/mismatched legacy file, an unresolvable S3 target conflict, an unmappable `mime_type`, or a `DELETING` Source with no provable owning `PipelineRun` — ADR-0011 D34 Case D) — readiness stays blocked until an operator resolves the underlying condition; this is a genuine "needs a human" signal, not a transient retry case. |
| `storage_convergence_awaiting_recovery_owned` | Convergence is otherwise clean but is waiting for an existing Forget/Dataset-DELETE run to reach its own terminal state — normal, expected during that run's own retry/backoff window. |
| `storage_convergence_fixed_point_reached` | Convergence completed this pass; the process is about to (or already did) become `OPERATIONAL`. |

**What to look for by symptom:**

- **Stuck at `not_ready`, no S3 errors in the log:** check for
  `bootstrap_schema_not_ready` — run the explicit migration (§3).
- **`bootstrap_attempt_failed` repeating with an S3-shaped cause:** confirm
  `STORAGE_S3_ENDPOINT_URL`/`STORAGE_S3_BUCKET`/`STORAGE_S3_REGION` and
  network reachability from the container to that endpoint (§13.8).
- **`bootstrap_attempt_failed` / IAM-shaped denial:** re-check the IAM
  policy against §13.5 — a missing `s3:ListBucket`/`s3:GetBucketVersioning`
  grant is a common gap since those are bucket-level, not object-level,
  permissions.
- **`storage_convergence_integrity_failures` with a Case-A-shaped message
  (missing/mismatched legacy file):** the local `/data/sources` object for
  that Source is gone or corrupted — this requires manual operator
  diagnosis (ADR-0011 does not define an automatic recovery for this case);
  do not attempt to fabricate a replacement file.
- **`storage_convergence_integrity_failures` with a Case-D-shaped message
  (`DELETING` Source, no provable lineage):** a `pipeline_runs`
  bookkeeping/historical-data problem, not a storage problem — this needs
  operator investigation of that Source's `PipelineRun` history, not a
  storage-layer fix.
- **Cleanup reported as deferred/unresolved
  (`storage_unresolved` metric on a Forget/Dataset-DELETE run):** expected
  and non-blocking (§13.6) — the run itself still succeeded; track the
  metric if your compliance posture needs guaranteed physical purge.
