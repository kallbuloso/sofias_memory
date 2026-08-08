# Sofias Memory

Sofias Memory is a focused, single-user semantic memory and knowledge graph service.

This repository is currently in the foundation phase. The FastAPI application
foundation, health endpoints, API key middleware, request correlation, and initial
Docker/Compose packaging are present. PostgreSQL/Neo4j clients, migrations, and worker
behavior are implemented in later phases.

Canonical product specification:

```text
docs/product/Sofias_Memory_PRD_SPECS.md
```

Active execution plans:

```text
docs/exec-plans/active/
```

## Development

Use `uv` for dependency management and local commands.

Common checks:

```bash
uv sync --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy sofias_memory scripts
uv run pytest tests/unit
```

Run the application on the host with the official factory entrypoint:

```bash
uv run uvicorn sofias_memory.app:create_app --factory --host 127.0.0.1 --port 8000
```

## Local Database Stack

The current development database stack is:

```text
sofias_memory_db
```

PostgreSQL:

```text
host = 127.0.0.1
published port = 5440
container port = 5432
database = cognee_db
user = cognee
password = DB_PASSWORD
```

Example host development URL:

```env
DATABASE_URL=postgresql+asyncpg://cognee:<DB_PASSWORD>@127.0.0.1:5440/cognee_db
```

Neo4j:

```text
host = 127.0.0.1
published Bolt port = 7688
container Bolt port = 7687
database = neo4j
user = neo4j
password = DB_NEO4J_PASSWORD
```

Example host development settings:

```env
NEO4J_URI=bolt://127.0.0.1:7688
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=<DB_NEO4J_PASSWORD>
NEO4J_DATABASE=neo4j
```

Known local stack issue:

```text
NEO4J_AUTH=neo4j/${DB_NEO4J_PASSWORD}
```

Therefore the Neo4j healthcheck must also use:

```text
${DB_NEO4J_PASSWORD}
```

and not:

```text
${DB_PASSWORD}
```

Ports `5440` and `7688` are host-published development ports only. Do not hard-code
them in Python code.

In the official Compose network, internal service communication remains:

```text
postgres:5432
neo4j:7687
```

## Docker Compose

`compose.yaml` is the canonical, portable stack definition. It contains:

- `sofias-memory`: the single Python application container;
- `postgres`: PostgreSQL 17 with pgvector;
- `neo4j`: Neo4j 5.26.

The application is the only service published to the host by default. PostgreSQL and
Neo4j remain on the internal Compose network.

Compose uses external interpolation variables for infrastructure passwords:

```text
DB_PASSWORD
DB_NEO4J_PASSWORD
```

Those names are intentionally not Sofias Memory application Settings. The app container
receives only declared Settings such as `DATABASE_URL`, `NEO4J_PASSWORD`, `API_KEY`,
and `LLM_API_KEY`. This keeps `extra="forbid"` meaningful for application `.env`
loading.

Example validation/run environment:

```bash
API_KEY=sf-replace-with-at-least-32-url-safe-random-chars
LLM_API_KEY=sk-replace-me
DB_PASSWORD=replace-me
DB_NEO4J_PASSWORD=replace-me-too
```

The app container uses:

```text
DATABASE_URL=postgresql+asyncpg://...@postgres:5432/...
NEO4J_URI=bolt://neo4j:7687
```

The host development ports `5440` and `7688` are only for running the Python process on
the host against the separate local database stack. Do not put those host ports in
Python code or internal Compose URLs.

## Portainer

Portainer currently uses `compose.yaml` directly as the Stack definition. Provide
secrets and environment variables through the Stack environment variables UI.

There is no Portainer override file at this stage because no project-specific
Portainer Compose difference is required. Do not expose PostgreSQL or Neo4j host ports
just to create a platform-specific file.

## EasyPanel

EasyPanel currently starts from `compose.yaml` through its Compose Service support.
Provide secrets and environment variables in the EasyPanel environment/configuration
area.

Domain, TLS, and proxy configuration belong to the EasyPanel UI and are not hard-coded
in this repository. There is no EasyPanel override file at this stage because no
project-specific EasyPanel Compose difference is required.
