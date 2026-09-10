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
        "sessions",
        "session_entries",
        "skills",
        "skill_revisions",
        "agents",
        "agent_skills",
        "agent_sessions",
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
                "sessions",
                "session_entries",
                "skills",
                "skill_revisions",
                "agents",
                "agent_skills",
                "agent_sessions",
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


# --- SM-804: non-attribution structural guard ----------------------------------


NON_ATTRIBUTION_GUARDED_TABLES = ("queries", "pipeline_runs", "session_entries")
"""ADR-0014 (SM-804): Agent<->Session is a management association, never a
per-operation attribution mechanism. Query/PipelineRun/SessionEntry provenance
(ADR-0012) is preserved unchanged and must never grow an `agent_id` column or
an FK to `agents`/`agent_sessions` -- doing so would let a caller attribute a
specific operation to a specific Agent, which the M:N Agent<->Session
cardinality makes structurally impossible to do correctly."""


@pytest.mark.integration
@pytest.mark.asyncio
async def test_query_pipeline_run_session_entry_never_gain_agent_identity() -> None:
    """Structural, real-PostgreSQL proof: `queries`, `pipeline_runs`, and
    `session_entries` have no `agent_id` column and no foreign key of any
    kind targeting `agents` or `agent_sessions`. This is checked directly
    against information_schema/pg_catalog, not by convention, so it survives
    future schema alterations that don't touch this test file."""

    if os.environ.get(POSTGRES_SCHEMA_GUARD_ENV) != "1":
        pytest.skip(f"set {POSTGRES_SCHEMA_GUARD_ENV}=1 to run the PostgreSQL schema guard")

    settings = load_settings()
    engine = create_async_engine_from_settings(settings)
    try:
        async with engine.connect() as connection:
            for table_name in NON_ATTRIBUTION_GUARDED_TABLES:
                column_result = await connection.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = :table_name
                          AND column_name = 'agent_id'
                        """
                    ),
                    {"table_name": table_name},
                )
                assert column_result.first() is None, f"{table_name}.agent_id must not exist"

                fk_result = await connection.execute(
                    text(
                        """
                        SELECT con.conname
                        FROM pg_catalog.pg_constraint AS con
                        JOIN pg_catalog.pg_class AS rel ON rel.oid = con.conrelid
                        JOIN pg_catalog.pg_class AS frel ON frel.oid = con.confrelid
                        WHERE rel.relname = :table_name
                          AND con.contype = 'f'
                          AND frel.relname IN ('agents', 'agent_sessions')
                        """
                    ),
                    {"table_name": table_name},
                )
                assert fk_result.first() is None, (
                    f"{table_name} must have no FK to agents/agent_sessions"
                )
    finally:
        await dispose_async_engine(engine)
