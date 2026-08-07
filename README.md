# Sofias Memory

Sofias Memory is a focused, single-user semantic memory and knowledge graph service.

This repository is currently in the B0 foundation phase. Runtime application code,
database migrations, Docker runtime behavior, and API implementation are intentionally
not implemented yet.

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
