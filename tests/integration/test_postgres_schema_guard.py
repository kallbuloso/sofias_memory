from __future__ import annotations

import os
from dataclasses import dataclass

import pytest
from sqlalchemy import text

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    dispose_async_engine,
)

POSTGRES_SCHEMA_GUARD_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_SCHEMA_GUARD"

FORBIDDEN_TABLES = frozenset(
    {
        "users",
        "roles",
        "permissions",
        "acl",
        "api_keys",
        "settings",
        "tenants",
    }
)
FORBIDDEN_COLUMNS = frozenset({"owner_id", "tenant_id"})
REQUIRED_EXTENSIONS = frozenset({"vector", "pg_trgm", "citext"})
REQUIRED_TABLES = frozenset(
    {
        "datasets",
        "sources",
        "documents",
        "chunks",
        "entities",
        "entity_mentions",
        "relations",
        "relation_evidence",
        "summaries",
        "memory_entries",
        "queries",
        "feedback",
        "pipeline_runs",
        "pipeline_steps",
        "graph_outbox",
    }
)


@dataclass(frozen=True)
class ColumnReference:
    table_name: str
    column_name: str


@dataclass(frozen=True)
class SchemaSnapshot:
    tables: frozenset[str]
    columns: frozenset[ColumnReference]
    extensions: frozenset[str]


def format_names(names: set[str] | frozenset[str]) -> str:
    return ", ".join(sorted(names))


def schema_guard_failures(snapshot: SchemaSnapshot) -> list[str]:
    failures: list[str] = []

    forbidden_tables_found = snapshot.tables & FORBIDDEN_TABLES
    if forbidden_tables_found:
        failures.append(
            f"Forbidden PostgreSQL tables found: {format_names(forbidden_tables_found)}"
        )

    forbidden_columns_found = frozenset(
        f"{column.table_name}.{column.column_name}"
        for column in snapshot.columns
        if column.column_name in FORBIDDEN_COLUMNS
    )
    if forbidden_columns_found:
        failures.append(
            f"Forbidden PostgreSQL columns found: {format_names(forbidden_columns_found)}"
        )

    missing_extensions = REQUIRED_EXTENSIONS - snapshot.extensions
    if missing_extensions:
        failures.append(
            f"Missing required PostgreSQL extensions: {format_names(missing_extensions)}"
        )

    missing_tables = REQUIRED_TABLES - snapshot.tables
    if missing_tables:
        failures.append(f"Missing required PostgreSQL tables: {format_names(missing_tables)}")

    return failures


async def load_schema_snapshot() -> SchemaSnapshot:
    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        async with engine.connect() as connection:
            schema_result = await connection.execute(text("SELECT current_schema()"))
            schema = str(schema_result.scalar_one())

            table_result = await connection.execute(
                text(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                      AND table_type = 'BASE TABLE'
                    """
                ),
                {"schema": schema},
            )
            tables = frozenset(str(row.table_name) for row in table_result)

            column_result = await connection.execute(
                text(
                    """
                    SELECT c.table_name, c.column_name
                    FROM information_schema.columns AS c
                    JOIN information_schema.tables AS t
                      ON t.table_schema = c.table_schema
                     AND t.table_name = c.table_name
                    WHERE c.table_schema = :schema
                      AND t.table_type = 'BASE TABLE'
                    """
                ),
                {"schema": schema},
            )
            columns = frozenset(
                ColumnReference(
                    table_name=str(row.table_name),
                    column_name=str(row.column_name),
                )
                for row in column_result
            )

            extension_result = await connection.execute(
                text("SELECT extname FROM pg_catalog.pg_extension")
            )
            extensions = frozenset(str(row.extname) for row in extension_result)
    finally:
        await dispose_async_engine(engine)

    return SchemaSnapshot(tables=tables, columns=columns, extensions=extensions)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_schema_guard_against_real_database() -> None:
    if os.environ.get(POSTGRES_SCHEMA_GUARD_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SCHEMA_GUARD_ENV}=1 to run the PostgreSQL schema guard")

    snapshot = await load_schema_snapshot()

    assert schema_guard_failures(snapshot) == []


def test_schema_guard_policy_constants_are_exact() -> None:
    assert (
        frozenset({"users", "roles", "permissions", "acl", "api_keys", "settings", "tenants"})
        == FORBIDDEN_TABLES
    )
    assert frozenset({"owner_id", "tenant_id"}) == FORBIDDEN_COLUMNS
    assert frozenset({"vector", "pg_trgm", "citext"}) == REQUIRED_EXTENSIONS
    assert (
        frozenset(
            {
                "datasets",
                "sources",
                "documents",
                "chunks",
                "entities",
                "entity_mentions",
                "relations",
                "relation_evidence",
                "summaries",
                "memory_entries",
                "queries",
                "feedback",
                "pipeline_runs",
                "pipeline_steps",
                "graph_outbox",
            }
        )
        == REQUIRED_TABLES
    )


def test_schema_guard_rejects_forbidden_tables_with_deterministic_message() -> None:
    snapshot = SchemaSnapshot(
        tables=REQUIRED_TABLES | {"users", "roles"},
        columns=frozenset(),
        extensions=REQUIRED_EXTENSIONS,
    )

    assert schema_guard_failures(snapshot) == ["Forbidden PostgreSQL tables found: roles, users"]


def test_schema_guard_rejects_forbidden_columns_with_table_context() -> None:
    snapshot = SchemaSnapshot(
        tables=REQUIRED_TABLES,
        columns=frozenset(
            {
                ColumnReference("documents", "owner_id"),
                ColumnReference("sources", "tenant_id"),
            }
        ),
        extensions=REQUIRED_EXTENSIONS,
    )

    assert schema_guard_failures(snapshot) == [
        "Forbidden PostgreSQL columns found: documents.owner_id, sources.tenant_id"
    ]


def test_schema_guard_rejects_missing_required_extensions() -> None:
    snapshot = SchemaSnapshot(
        tables=REQUIRED_TABLES,
        columns=frozenset(),
        extensions=frozenset({"citext", "pg_trgm"}),
    )

    assert schema_guard_failures(snapshot) == ["Missing required PostgreSQL extensions: vector"]


def test_schema_guard_rejects_missing_required_tables() -> None:
    snapshot = SchemaSnapshot(
        tables=REQUIRED_TABLES - {"graph_outbox"},
        columns=frozenset(),
        extensions=REQUIRED_EXTENSIONS,
    )

    assert schema_guard_failures(snapshot) == ["Missing required PostgreSQL tables: graph_outbox"]


def test_schema_guard_uses_subset_semantics_and_allows_extra_objects() -> None:
    snapshot = SchemaSnapshot(
        tables=REQUIRED_TABLES | {"future_legitimate_table"},
        columns=frozenset(
            {
                ColumnReference("documents", "id"),
                ColumnReference("documents", "dataset_id"),
                ColumnReference("sources", "source_id"),
            }
        ),
        extensions=REQUIRED_EXTENSIONS | {"plpgsql"},
    )

    assert schema_guard_failures(snapshot) == []
