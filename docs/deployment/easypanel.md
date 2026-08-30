# Easypanel Deployment (EASYPANEL-001)

This is the proven, canonical guide for deploying Sofias Memory on Easypanel
from the already-published, immutable release image
`ghcr.io/kallbuloso/sofias-memory:0.1.2` — no build, no source checkout, no
`Dockerfile` is required on the Easypanel host. The Compose definition used is
[`deploy/easypanel/compose.yaml`](../../deploy/easypanel/compose.yaml), a
deployment-only variant of the root [`compose.yaml`](../../compose.yaml) with
the same three-service topology, database images, security posture, and
environment contract — the only differences are `image:` instead of `build:`,
and no host port published for `sofias-memory` (Easypanel routes its domain
directly to the container's internal port instead).

For maximum reproducibility (e.g. verifying exactly what was validated below),
the exact image may optionally be pinned by digest instead of tag:
`ghcr.io/kallbuloso/sofias-memory@sha256:324f0d912f955257271442238a4fff42ae21c3ff234a9e8e1586537e4c917421`
(the same digest published for the `0.1.2` tag). The canonical
`deploy/easypanel/compose.yaml` itself uses the exact version tag, not the
digest — see `docs/operations.md` §D for the general version/digest pinning
policy.

This guide does not repeat the general operational contract (migration
policy, backup/restore, rollback) — see [`docs/operations.md`](../operations.md)
for that. It only covers what is specific to installing on Easypanel.

**Key facts:**

- Migrations are a first-install / schema-upgrade operation, run explicitly
  (§4) — never applied automatically at application startup.
- The three named volumes (`sofias_memory_postgres_data`,
  `sofias_memory_neo4j_data`, `sofias_memory_sources`) persist all durable
  state across restarts and redeploys.
- No host port is required (or published) for `postgres` or `neo4j` — only
  `sofias-memory`'s internal port 8000 is ever exposed, via the Easypanel
  domain.
- In `production` (the default `APP_ENV`), `/docs` and `/openapi.json`
  return `404` — the documentation surface does not exist outside
  `APP_ENV=dev`/`development` (SWAGGER-001).
- Every API endpoint other than `/health/live` and `/health/ready` requires
  the `X-API-Key` header.

## 1. Create the Compose service

In Easypanel: **New Project** → **Compose** (or add a Compose service to an
existing project). Two sources are valid:

**Option A — Git source (preferred for a normal install).**
`deploy/easypanel/compose.yaml` is committed to the repository, so Easypanel
can pull it directly:

```text
repository:    https://github.com/kallbuloso/sofias_memory
branch:        main
build path:    /
compose file:  deploy/easypanel/compose.yaml
```

A specific stable tag (e.g. `v0.1.2`) may be used instead of `main` for a
pinned, reproducible source reference.

**Option B — Inline Compose.** Select Easypanel's **Inline**/**paste Compose
content** source option and paste the full contents of
[`deploy/easypanel/compose.yaml`](../../deploy/easypanel/compose.yaml)
directly into the editor. This remains a valid choice — for example, when an
Easypanel instance has no outbound access to GitHub, or when trialing a local
edit before committing it.

## 2. Configure environment variables

Set the following in Easypanel's Compose environment editor (never commit
real values; `.env.example` documents the application's own `Settings`
shape for reference, but the variables below are the Compose-level
interpolation inputs `deploy/easypanel/compose.yaml` actually consumes):

Required (the stack refuses to start without these):

```text
API_KEY
DB_PASSWORD
DB_NEO4J_PASSWORD
LLM_API_KEY
```

Generate `API_KEY` with `scripts/generate_api_key.py` from a source checkout,
or any equivalent random 32+ character generator with the `sf-` prefix.
`DB_PASSWORD`/`DB_NEO4J_PASSWORD` should be strong, unique values distinct
from any other deployment.

**For a first install specifically, also set:**

```text
WORKER_ENABLED=false
```

temporarily — see step 4 for why. Step 4 has you set it back to `true` (or
unset it, since `true` is the default) once the schema is migrated.

Optional but commonly set:

```text
EMBEDDING_API_KEY   # only if your embedding provider needs a separate key
LLM_MODEL, EMBEDDING_MODEL, EMBEDDING_DIMENSIONS  # if not using the defaults
```

Every other variable in `deploy/easypanel/compose.yaml` has a documented
default and does not need to be set for a first install.

## 3. Deploy — without a public domain yet

Deploy the stack now, but **do not configure/expose the Easypanel domain
yet** (skip step 5 for now). `sofias-memory` has `depends_on: condition:
service_healthy` on both `postgres` and `neo4j`, so it will not start
running until both databases report healthy. With `WORKER_ENABLED=false`
set in step 2, the application starts read-only-safe with no worker
attempting to process any (nonexistent, on a fresh install) durable work
against a not-yet-migrated schema.

Wait until Easypanel reports `postgres` and `neo4j` as healthy before
continuing.

## 4. Apply migrations explicitly — required before first use

**Do not skip this. Migrations are never applied automatically**, and
`deploy/easypanel/compose.yaml` deliberately has no auto-migration service —
see `docs/operations.md` §3 for the general contract this follows.

**This procedure is specifically for a FIRST INSTALL against an empty
database.** It does not redefine the general upgrade/migration contract in
`docs/operations.md` §4 — a later upgrade of an already-migrated deployment
should still follow that document's backup-first procedure.

1. Confirm `postgres` and `neo4j` are healthy (step 3) and `WORKER_ENABLED=false`
   is set (step 2).
2. Open Easypanel's browser console/terminal for the running `sofias-memory`
   container (the exact UI affordance depends on your Easypanel version —
   look for a "Console," "Terminal," or "Shell" action on the service).
3. Inside that console, run directly (the release image already has its
   virtualenv's `bin/` on `PATH`, so no `uv run` prefix is needed):

   ```bash
   alembic upgrade head
   ```

4. Confirm the result, both should report `0011 (head)`:

   ```bash
   alembic current
   alembic heads
   ```

5. Back in Easypanel's environment editor, set `WORKER_ENABLED=true` (or
   remove the variable, since `true` is the default).
6. Redeploy/restart the `sofias-memory` service so it picks up the new
   environment value.

## 5. Confirm health, then configure the domain

Before exposing any public domain, confirm readiness using Easypanel's
internal network or a temporary port-forward/console `curl`:

```bash
curl -sS http://localhost:8000/health/live
curl -sS http://localhost:8000/health/ready
```

`/health/ready` must report `"status": "ready"` with all three checks
(`postgres`, `neo4j`, `worker`) ready. Only once this passes, configure the
Easypanel domain/routing for this project:

- **Service**: `sofias-memory`
- **Port**: `8000`
- **Protocol**: `HTTP`

Do not expose `postgres` or `neo4j` on any Easypanel domain or host port —
`deploy/easypanel/compose.yaml` never publishes a host port for either, and
the only intended public surface is the application's HTTP domain.

## 6. Verify the deployment via the public domain

Using the domain Easypanel assigned (`$DOMAIN` below):

```bash
curl -sS "https://$DOMAIN/health/live"
curl -sS "https://$DOMAIN/health/ready"
curl -sS -H "X-API-Key: $API_KEY" "https://$DOMAIN/api/v1/info"
```

## 7. Verify production Swagger/OpenAPI behavior

`APP_ENV` defaults to `production` in `deploy/easypanel/compose.yaml`
(matching the canonical Compose default), so the documentation surface must
be entirely absent:

```bash
curl -sS -o /dev/null -w "%{http_code}\n" "https://$DOMAIN/docs"        # expect 404
curl -sS -o /dev/null -w "%{http_code}\n" "https://$DOMAIN/openapi.json" # expect 404
```

Both must return `404`, not an auth error — the routes are genuinely not
registered outside `APP_ENV=dev`/`development` (SWAGGER-001).

## 8. Production smoke

From a machine with network access to the deployed domain and a real LLM/
embedding provider configured, run the packaged smoke script from a source
checkout (see `docs/operations.md` §12.H for the full contract):

```bash
SOFIAS_MEMORY_API_KEY="$API_KEY" uv run python scripts/production_smoke.py \
  --base-url "https://$DOMAIN"
```

A `PRODUCTION SMOKE PASS` confirms Remember, Cognify (via `mode=full`),
Recall, and administrative Dataset delete are all functioning end-to-end
against the real deployment.

## Validation evidence

This procedure has been executed against a real Easypanel deployment, not
merely reviewed:

- Full installation (§1-§7) PASS, including `/health/live`, `/health/ready`
  with PostgreSQL, Neo4j, and the worker all reported ready, and
  `/api/v1/info` reporting the running version under `production`.
- `v0.1.2-rc.1`'s complete `production_smoke.py` suite PASS end to end
  (Remember, Cognify, Recall).
- A fresh administrative Dataset DELETE PASS.
- A Dataset left `failed`/`deleting` by the `v0.1.1` production incident
  that motivated the `v0.1.2` fix was recovered to `Dataset.status=DELETED`
  through the supported run retry flow (`POST /api/v1/runs/{run_id}/retry`)
  — with no manual PostgreSQL, Neo4j, `graph_outbox`, or filesystem repair.
