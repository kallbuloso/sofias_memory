# Development Guide

This document covers running Sofias Memory directly on a developer machine against
a local database stack, running checks/tests, and the caveats specific to that local
setup. It is not a production deployment guide — see [`README.md`](../README.md) for
the product overview and [`docs/api.md`](api.md) for API semantics. Deployment guides
covering Docker/Portainer/EasyPanel in more depth are planned for a later release
task (see `docs/exec-plans/active/Sofias_Memory_Release_v0.1.0_Backlog.md`, REL-006).

## Toolchain

Use [`uv`](https://docs.astral.sh/uv/) for dependency management and running project
commands. Python `>=3.12,<3.13` is required (see `pyproject.toml`).

```bash
uv sync --dev
```

## Quality checks

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory scripts
uv run pytest tests/unit
```

Integration tests that require real PostgreSQL/Neo4j are opt-in via dedicated
environment variables per test module (each integration test file documents its own
`SOFIAS_MEMORY_RUN_*_TESTS` and `*_TEST_DATABASE_URL` variables, and expects a
dedicated, discardable database — never point one at a database with data you care
about). Running the full opt-in suite requires setting many such variables at once;
consult the individual test files under `tests/integration/` for the exact names
before running them, rather than exporting variables blindly. A test marked
`@pytest.mark.integration` will `pytest.skip` with an explanation if its
prerequisites are not set, so a plain `uv run pytest` is always safe to run without
any real infrastructure — the unit suite alone runs no external I/O.

## Running the application on the host

```bash
uv run uvicorn sofias_memory.app:create_app --factory --host 127.0.0.1 --port 8000
```

This requires a populated `.env` (see `.env.example`) pointing at a reachable
PostgreSQL (migrated to the current Alembic head) and Neo4j instance, plus a valid
`API_KEY` and LLM/embedding provider credentials. See
[`README.md`](../README.md#configuration) for the Settings overview.

## Current local development database stack

This is one specific developer's local stack layout — it is **not** a universal
contract of the product, and none of these host ports belong in application code or
in the canonical `compose.yaml` internal network. It exists here only so a
contributor can reproduce a working local environment quickly.

Stack name: `sofias_memory_db`.

PostgreSQL:

```text
host            = 127.0.0.1
published port  = 5440
container port  = 5432
database        = cognee_db
user            = cognee
password        = DB_PASSWORD
```

Example host development URL:

```env
DATABASE_URL=postgresql+asyncpg://cognee:<DB_PASSWORD>@127.0.0.1:5440/cognee_db
```

Neo4j:

```text
host              = 127.0.0.1
published Bolt port = 7688
container Bolt port = 7687
database          = neo4j
user              = neo4j
password          = DB_NEO4J_PASSWORD
```

Example host development settings:

```env
NEO4J_URI=bolt://127.0.0.1:7688
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<DB_NEO4J_PASSWORD>
NEO4J_DATABASE=neo4j
```

**Known local stack caveat:** if your local stack sets

```text
NEO4J_AUTH=neo4j/${DB_NEO4J_PASSWORD}
```

then the Neo4j healthcheck for that stack must also use `${DB_NEO4J_PASSWORD}`, not
`${DB_PASSWORD}` — a mismatch here produces a Neo4j container that starts but is
reported unhealthy. This is specific to how a local, hand-rolled dev stack is
configured; the canonical `compose.yaml` in this repository does not have this
issue (its Neo4j service already uses `DB_NEO4J_PASSWORD` consistently for both
`NEO4J_AUTH` and its healthcheck).

Ports `5440` and `7688` are host-published development ports only — never hard-code
them in Python code. When running against the canonical `compose.yaml` network
instead, internal service communication is:

```text
postgres:5432
neo4j:7687
```

## Operational scripts

These scripts run from a source checkout via `uv run python scripts/...` as
shown below. They are also packaged inside the release image itself (see
`docs/operations.md`), so the same scripts run there too, with no source
checkout needed — e.g. `docker run --rm --entrypoint uv sofias-memory:0.1.1
run --no-sync python scripts/rebuild_graph.py --all --confirm-all`.

```bash
# Generate a valid API_KEY value
uv run python scripts/generate_api_key.py

# Verify the local environment can import the package and has a supported Python
uv run python scripts/verify_installation.py

# Rebuild the Neo4j projection for one dataset, or all datasets, from PostgreSQL
uv run python scripts/rebuild_graph.py --dataset <uuid>
uv run python scripts/rebuild_graph.py --all
```

## Notes on Portainer/EasyPanel during development

`compose.yaml` is the canonical, portable stack definition and is what both
Portainer and EasyPanel consume directly — there is no development-specific
override file for either platform, and none should be added just to expose the
database ports above. Full production deployment guidance for these platforms is
tracked as later release work (REL-006); this document only concerns local
development, not deployment.
