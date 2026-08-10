from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from sofias_memory.config import load_settings
from tests.integration.test_postgres_schema_guard import (
    FORBIDDEN_COLUMNS,
    FORBIDDEN_TABLES,
    REQUIRED_EXTENSIONS,
    REQUIRED_TABLES,
    ColumnReference,
    SchemaSnapshot,
    schema_guard_failures,
)

POSTGRES_MIGRATION_GATE_ENV = "SOFIAS_MEMORY_RUN_POSTGRES_MIGRATION_GATE"
POSTGRES_MIGRATION_GATE_BACKEND_ENV = "SOFIAS_MEMORY_POSTGRES_GATE_BACKEND"
POSTGRES_GATE_BACKEND_LOCAL = "local"
POSTGRES_GATE_BACKEND_TESTCONTAINERS = "testcontainers"
POSTGRES_GATE_BACKENDS = frozenset(
    {POSTGRES_GATE_BACKEND_LOCAL, POSTGRES_GATE_BACKEND_TESTCONTAINERS}
)
TEMPORARY_DATABASE_PREFIX = "sofias_memory_gate_"
TEMPORARY_DATABASE_PATTERN = re.compile(r"^sofias_memory_gate_[a-f0-9]{32}$")
EXPECTED_EMBEDDING_DIMENSIONS = 3072
PROJECT_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = PROJECT_ROOT / "compose.yaml"
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"

EXPECTED_PRIMARY_KEYS = frozenset(f"pk_{table_name}" for table_name in REQUIRED_TABLES)
EXPECTED_UNIQUE_CONSTRAINTS = frozenset(
    {
        "uq_datasets_name",
        "uq_datasets_slug",
        "uq_sources_dataset_id_content_sha256_version",
        "uq_chunks_document_id_generation_ordinal",
    }
)
EXPECTED_CHECK_CONSTRAINTS = frozenset(
    {
        "ck_datasets_name_max_length",
        "ck_feedback_score_allowed_values",
        "ck_pipeline_runs_payload_hash_hex",
        "ck_pipeline_runs_config_fingerprint_hex",
        "ck_pipeline_runs_attempt_non_negative",
        "ck_pipeline_steps_attempt_non_negative",
        "ck_graph_outbox_attempt_non_negative",
    }
)
EXPECTED_FK_DELETE_POLICIES = {
    "fk_pipeline_runs_dataset_id_datasets": "SET NULL",
    "fk_pipeline_runs_source_id_sources": "SET NULL",
    "fk_pipeline_steps_run_id_pipeline_runs": "CASCADE",
    "fk_memory_entries_source_id_sources": "SET NULL",
    "fk_feedback_query_id_queries": "RESTRICT",
    "fk_relation_evidence_chunk_id_chunks": "RESTRICT",
    "fk_entity_mentions_entity_id_entities": "CASCADE",
    "fk_entity_mentions_chunk_id_chunks": "CASCADE",
}
POSTGRES_FK_DELETE_ACTION_NAMES = {
    "a": "NO ACTION",
    "r": "RESTRICT",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}
POSTGRES_CONSTRAINT_TYPE_CODES = frozenset({"p", "f", "u", "c"})
EXPECTED_INDEXES = frozenset(
    {
        "ix_chunks_lexical",
        "ix_chunks_dataset_id_is_active",
        "ix_chunks_source_id_is_active",
        "ix_chunks_embedding_halfvec_hnsw",
        "uq_entities_dataset_id_canonical_key_active",
        "ix_entity_mentions_entity_id",
        "ix_entity_mentions_chunk_id",
        "ix_relations_dataset_id_is_active",
        "ix_relations_source_entity_id",
        "ix_relations_target_entity_id",
        "ix_relation_evidence_chunk_id",
        "ix_summaries_dataset_id_generation_is_active",
        "ix_summaries_embedding_halfvec_hnsw",
        "ix_pipeline_runs_status",
        "ix_pipeline_runs_dataset_id_status",
        "ix_pipeline_runs_heartbeat_at",
        "uq_pipeline_runs_idempotency_key",
        "ix_pipeline_runs_created_at",
        "ix_pipeline_steps_run_id",
        "ix_pipeline_steps_run_id_ordinal",
        "ix_pipeline_steps_status",
        "ix_graph_outbox_status",
        "ix_graph_outbox_status_created_at",
        "ix_graph_outbox_dataset_id",
        "ix_graph_outbox_aggregate",
    }
)
EMBEDDING_COLUMNS = (
    ("chunks", "embedding"),
    ("entities", "embedding"),
    ("relations", "embedding"),
    ("summaries", "embedding"),
)


@dataclass(frozen=True)
class DisposableDatabaseUrl:
    _value: str = field(repr=False)

    def get_secret_value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "DisposableDatabaseUrl(<redacted>)"

    def __str__(self) -> str:
        return "<redacted>"


def compose_postgres_image() -> str:
    match = re.search(
        r"(?ms)^  postgres:\s*$.*?^    image:\s*(?P<image>\S+)\s*$",
        COMPOSE_FILE.read_text(encoding="utf-8"),
    )
    if match is None:
        raise AssertionError("compose.yaml does not define a postgres service image")
    return match.group("image")


def vector_literal(dimensions: int, *, hot_index: int = 0) -> str:
    values = ["0"] * dimensions
    values[hot_index] = "1"
    return f"[{','.join(values)}]"


def selected_postgres_gate_backend() -> str:
    backend = os.environ.get(POSTGRES_MIGRATION_GATE_BACKEND_ENV)
    if backend in POSTGRES_GATE_BACKENDS:
        return backend
    allowed_values = ", ".join(sorted(POSTGRES_GATE_BACKENDS))
    raise AssertionError(f"set {POSTGRES_MIGRATION_GATE_BACKEND_ENV} to one of: {allowed_values}")


def generate_temporary_database_name() -> str:
    return f"{TEMPORARY_DATABASE_PREFIX}{uuid4().hex}"


def configured_database_name(database_url: str) -> str:
    parsed = urlsplit(database_url)
    database_name = unquote(parsed.path.lstrip("/"))
    if not database_name:
        raise AssertionError("configured PostgreSQL URL must include a database name")
    return database_name


def build_temporary_database_url(database_url: str, temporary_database_name: str) -> str:
    assert_safe_temporary_database_name(
        temporary_database_name,
        configured_database_name=configured_database_name(database_url),
    )
    parsed = urlsplit(database_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"/{quote(temporary_database_name)}",
            parsed.query,
            parsed.fragment,
        )
    )


def assert_safe_temporary_database_name(
    temporary_database_name: str,
    *,
    configured_database_name: str,
) -> None:
    if temporary_database_name == configured_database_name:
        raise AssertionError("temporary PostgreSQL database must differ from configured database")
    assert_safe_drop_database_name(temporary_database_name)


def assert_safe_drop_database_name(database_name: str) -> None:
    if TEMPORARY_DATABASE_PATTERN.fullmatch(database_name) is None:
        raise AssertionError("refusing to drop PostgreSQL database without gate-safe prefix")


def quote_postgres_identifier(identifier: str) -> str:
    assert_safe_drop_database_name(identifier)
    return f'"{identifier.replace('"', '""')}"'


def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


def code_heads(config: Config) -> frozenset[str]:
    return frozenset(ScriptDirectory.from_config(config).get_heads())


def single_code_head(config: Config) -> str:
    heads = code_heads(config)
    assert len(heads) == 1
    return next(iter(heads))


def single_down_revision(config: Config, revision: str) -> str:
    script = ScriptDirectory.from_config(config)
    script_revision = script.get_revision(revision)
    assert script_revision is not None
    down_revision = script_revision.down_revision
    if isinstance(down_revision, tuple):
        assert len(down_revision) == 1
        return down_revision[0]
    assert isinstance(down_revision, str)
    return down_revision


@pytest.fixture()
def disposable_postgres_url(monkeypatch: pytest.MonkeyPatch) -> Iterator[DisposableDatabaseUrl]:
    if os.environ.get(POSTGRES_MIGRATION_GATE_ENV) != "1":
        pytest.skip(f"set {POSTGRES_MIGRATION_GATE_ENV}=1 to run the PostgreSQL migration gate")

    try:
        backend = selected_postgres_gate_backend()
    except AssertionError as exc:
        pytest.fail(str(exc))

    if backend == POSTGRES_GATE_BACKEND_LOCAL:
        yield from _local_disposable_postgres_url(monkeypatch)
        return

    if backend == POSTGRES_GATE_BACKEND_TESTCONTAINERS:
        yield from _testcontainers_disposable_postgres_url(monkeypatch)
        return

    pytest.fail(f"unsupported PostgreSQL migration gate backend: {backend}")


def _local_disposable_postgres_url(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[DisposableDatabaseUrl]:
    settings = load_settings()
    configured_url = settings.database_url.get_secret_value()
    original_database_name = configured_database_name(configured_url)
    temporary_database_name = generate_temporary_database_name()
    assert_safe_temporary_database_name(
        temporary_database_name,
        configured_database_name=original_database_name,
    )
    temporary_database_url = build_temporary_database_url(configured_url, temporary_database_name)

    asyncio.run(create_local_disposable_database(configured_url, temporary_database_name))
    try:
        _configure_test_settings(monkeypatch, temporary_database_url)
        yield DisposableDatabaseUrl(temporary_database_url)
    finally:
        asyncio.run(drop_local_disposable_database(configured_url, temporary_database_name))


def _testcontainers_disposable_postgres_url(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[DisposableDatabaseUrl]:
    from testcontainers.postgres import PostgresContainer

    container = PostgresContainer(
        compose_postgres_image(),
        username="sofias_memory_gate",
        password="sofias_memory_gate",
        dbname="sofias_memory_gate",
        driver="asyncpg",
    )
    try:
        container.start()
    except Exception as exc:  # pragma: no cover - exercised only when Docker is unavailable.
        pytest.fail(f"Disposable PostgreSQL Testcontainer failed to start: {type(exc).__name__}")

    try:
        database_url = container.get_connection_url(driver="asyncpg")
        _configure_test_settings(monkeypatch, database_url)
        yield DisposableDatabaseUrl(database_url)
    finally:
        container.stop()


async def create_local_disposable_database(
    admin_database_url: str,
    temporary_database_name: str,
) -> None:
    assert_safe_temporary_database_name(
        temporary_database_name,
        configured_database_name=configured_database_name(admin_database_url),
    )
    engine = create_async_engine(admin_database_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(f"CREATE DATABASE {quote_postgres_identifier(temporary_database_name)}")
            )
    finally:
        await engine.dispose()


async def drop_local_disposable_database(
    admin_database_url: str,
    temporary_database_name: str,
) -> None:
    assert_safe_temporary_database_name(
        temporary_database_name,
        configured_database_name=configured_database_name(admin_database_url),
    )
    engine = create_async_engine(admin_database_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text(
                    """
                    SELECT pg_catalog.pg_terminate_backend(pid)
                    FROM pg_catalog.pg_stat_activity
                    WHERE datname = :database_name
                      AND pid <> pg_catalog.pg_backend_pid()
                    """
                ),
                {"database_name": temporary_database_name},
            )
            await connection.execute(
                text(
                    f"DROP DATABASE IF EXISTS {quote_postgres_identifier(temporary_database_name)}"
                )
            )
    finally:
        await engine.dispose()


def _configure_test_settings(monkeypatch: pytest.MonkeyPatch, database_url: str) -> None:
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("API_KEY", f"sf-{'A' * 32}")
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("DATABASE_POOL_SIZE", "2")
    monkeypatch.setenv("DATABASE_MAX_OVERFLOW", "0")
    monkeypatch.setenv("NEO4J_PASSWORD", "test-neo4j-password")
    monkeypatch.setenv("LLM_API_KEY", "test-llm-api-key")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", str(EXPECTED_EMBEDDING_DIMENSIONS))


@pytest.mark.integration
def test_postgres_migration_gate_from_empty_database(
    disposable_postgres_url: DisposableDatabaseUrl,
) -> None:
    config = alembic_config()
    head = single_code_head(config)
    predecessor = single_down_revision(config, head)

    asyncio.run(assert_database_starts_empty(disposable_postgres_url))

    command.upgrade(config, "head")
    asyncio.run(assert_complete_b2_schema(disposable_postgres_url, expected_revision=head))
    asyncio.run(exercise_minimal_b2_schema(disposable_postgres_url))

    command.downgrade(config, "-1")
    asyncio.run(
        assert_last_migration_downgraded(
            disposable_postgres_url,
            expected_revision=predecessor,
        )
    )

    command.upgrade(config, "head")
    asyncio.run(assert_complete_b2_schema(disposable_postgres_url, expected_revision=head))


async def database_connection(
    database_url: DisposableDatabaseUrl,
) -> AsyncIterator[AsyncConnection]:
    engine = create_async_engine(database_url.get_secret_value(), pool_pre_ping=True)
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


async def assert_database_starts_empty(database_url: DisposableDatabaseUrl) -> None:
    async for connection in database_connection(database_url):
        schema = await current_schema(connection)
        tables = await base_tables(connection, schema=schema)
        revisions = await database_revisions(connection, schema=schema)

    assert revisions == frozenset()
    assert "alembic_version" not in tables
    assert REQUIRED_TABLES.isdisjoint(tables)


async def assert_complete_b2_schema(
    database_url: DisposableDatabaseUrl,
    *,
    expected_revision: str,
) -> None:
    async for connection in database_connection(database_url):
        schema = await current_schema(connection)
        tables = await base_tables(connection, schema=schema)
        snapshot = SchemaSnapshot(
            tables=tables,
            columns=await base_table_columns(connection, schema=schema),
            extensions=await installed_extensions(connection),
        )
        revisions = await database_revisions(connection, schema=schema)
        constraints = await constraints_by_name(connection, schema=schema)
        foreign_keys = await foreign_key_delete_policies(connection, schema=schema)
        indexes = await indexes_by_name(connection, schema=schema)
        embedding_types = await embedding_column_types(connection, schema=schema)

    assert revisions == frozenset({expected_revision})
    assert schema_guard_failures(snapshot) == []
    assert snapshot.extensions >= REQUIRED_EXTENSIONS
    assert snapshot.tables >= REQUIRED_TABLES
    assert {
        name for name, details in constraints.items() if details["type"] == "p"
    } >= EXPECTED_PRIMARY_KEYS
    assert {
        name for name, details in constraints.items() if details["type"] == "u"
    } >= EXPECTED_UNIQUE_CONSTRAINTS
    assert {
        name for name, details in constraints.items() if details["type"] == "c"
    } >= EXPECTED_CHECK_CONSTRAINTS
    for fk_name, expected_on_delete in EXPECTED_FK_DELETE_POLICIES.items():
        assert foreign_keys[fk_name]["on_delete"] == expected_on_delete
    assert not any(details["table"] == "graph_outbox" for details in foreign_keys.values())
    assert indexes.keys() >= EXPECTED_INDEXES
    assert indexes["ix_chunks_lexical"]["method"] == "gin"
    assert indexes["ix_chunks_embedding_halfvec_hnsw"]["method"] == "hnsw"
    assert indexes["ix_summaries_embedding_halfvec_hnsw"]["method"] == "hnsw"
    assert_hnsw_halfvec_index(indexes["ix_chunks_embedding_halfvec_hnsw"]["definition"])
    assert_hnsw_halfvec_index(indexes["ix_summaries_embedding_halfvec_hnsw"]["definition"])
    assert_partial_unique_active_entity_index(
        indexes["uq_entities_dataset_id_canonical_key_active"]["definition"]
    )
    assert_partial_unique_idempotency_index(
        indexes["uq_pipeline_runs_idempotency_key"]["definition"]
    )
    assert embedding_types == {
        ("chunks", "embedding"): "vector(3072)",
        ("entities", "embedding"): "vector(3072)",
        ("relations", "embedding"): "vector(3072)",
        ("summaries", "embedding"): "vector(3072)",
    }


async def assert_last_migration_downgraded(
    database_url: DisposableDatabaseUrl,
    *,
    expected_revision: str,
) -> None:
    async for connection in database_connection(database_url):
        schema = await current_schema(connection)
        tables = await base_tables(connection, schema=schema)
        revisions = await database_revisions(connection, schema=schema)

    assert revisions == frozenset({expected_revision})
    assert {"pipeline_runs", "pipeline_steps", "graph_outbox"}.isdisjoint(tables)
    assert {"datasets", "chunks", "feedback"} <= tables


def assert_hnsw_halfvec_index(definition: str) -> None:
    normalized = definition.lower()
    assert "using hnsw" in normalized
    assert re.search(r"\(*\s*embedding\s*\)*::halfvec\(3072\)", normalized) is not None
    assert "halfvec_cosine_ops" in normalized
    assert "ivfflat" not in normalized
    assert "vector_l2_ops" not in normalized
    assert "vector_ip_ops" not in normalized
    assert "vector_cosine_ops" not in normalized


def assert_partial_unique_active_entity_index(definition: str) -> None:
    normalized = " ".join(definition.lower().split())
    assert "create unique index" in normalized
    assert "(dataset_id, canonical_key)" in normalized
    assert "where (is_active is true)" in normalized


def assert_partial_unique_idempotency_index(definition: str) -> None:
    normalized = " ".join(definition.lower().split())
    assert "create unique index" in normalized
    assert "(idempotency_key)" in normalized
    assert "where (idempotency_key is not null)" in normalized
    assert "payload_hash" not in normalized


def postgres_fk_delete_action_name(delete_action: str) -> str:
    return POSTGRES_FK_DELETE_ACTION_NAMES[delete_action]


def postgres_constraint_type_code(constraint_type: str) -> str:
    if constraint_type not in POSTGRES_CONSTRAINT_TYPE_CODES:
        raise KeyError(constraint_type)
    return constraint_type


async def current_schema(connection: AsyncConnection) -> str:
    result = await connection.execute(text("SELECT current_schema()"))
    return str(result.scalar_one())


async def base_tables(connection: AsyncConnection, *, schema: str) -> frozenset[str]:
    result = await connection.execute(
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
    return frozenset(str(row.table_name) for row in result)


async def base_table_columns(
    connection: AsyncConnection,
    *,
    schema: str,
) -> frozenset[ColumnReference]:
    result = await connection.execute(
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
    return frozenset(
        ColumnReference(table_name=str(row.table_name), column_name=str(row.column_name))
        for row in result
    )


async def installed_extensions(connection: AsyncConnection) -> frozenset[str]:
    result = await connection.execute(text("SELECT extname FROM pg_catalog.pg_extension"))
    return frozenset(str(row.extname) for row in result)


async def database_revisions(connection: AsyncConnection, *, schema: str) -> frozenset[str]:
    tables = await base_tables(connection, schema=schema)
    if "alembic_version" not in tables:
        return frozenset()

    result = await connection.execute(text("SELECT version_num FROM alembic_version"))
    return frozenset(str(row.version_num) for row in result)


async def constraints_by_name(
    connection: AsyncConnection,
    *,
    schema: str,
) -> dict[str, dict[str, str]]:
    result = await connection.execute(
        text(
            """
            SELECT
                con.conname AS name,
                con.contype::text AS constraint_type,
                rel.relname AS table_name,
                pg_catalog.pg_get_constraintdef(con.oid) AS definition
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS rel
              ON rel.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS nsp
              ON nsp.oid = rel.relnamespace
            WHERE nsp.nspname = :schema
            """
        ),
        {"schema": schema},
    )
    return {
        str(row.name): {
            "type": postgres_constraint_type_code(str(row.constraint_type)),
            "table": str(row.table_name),
            "definition": str(row.definition),
        }
        for row in result
    }


async def foreign_key_delete_policies(
    connection: AsyncConnection,
    *,
    schema: str,
) -> dict[str, dict[str, str]]:
    result = await connection.execute(
        text(
            """
            SELECT
                con.conname AS name,
                rel.relname AS table_name,
                con.confdeltype::text AS delete_action
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS rel
              ON rel.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS nsp
              ON nsp.oid = rel.relnamespace
            WHERE nsp.nspname = :schema
              AND con.contype = 'f'
            """
        ),
        {"schema": schema},
    )
    return {
        str(row.name): {
            "table": str(row.table_name),
            "on_delete": postgres_fk_delete_action_name(str(row.delete_action)),
        }
        for row in result
    }


async def indexes_by_name(
    connection: AsyncConnection,
    *,
    schema: str,
) -> dict[str, dict[str, str]]:
    result = await connection.execute(
        text(
            """
            SELECT
                idx.relname AS index_name,
                tbl.relname AS table_name,
                am.amname AS method,
                pg_catalog.pg_get_indexdef(ix.indexrelid) AS definition
            FROM pg_catalog.pg_index AS ix
            JOIN pg_catalog.pg_class AS idx
              ON idx.oid = ix.indexrelid
            JOIN pg_catalog.pg_class AS tbl
              ON tbl.oid = ix.indrelid
            JOIN pg_catalog.pg_namespace AS nsp
              ON nsp.oid = tbl.relnamespace
            JOIN pg_catalog.pg_am AS am
              ON am.oid = idx.relam
            WHERE nsp.nspname = :schema
            """
        ),
        {"schema": schema},
    )
    return {
        str(row.index_name): {
            "table": str(row.table_name),
            "method": str(row.method),
            "definition": str(row.definition),
        }
        for row in result
    }


async def embedding_column_types(
    connection: AsyncConnection,
    *,
    schema: str,
) -> dict[tuple[str, str], str]:
    result = await connection.execute(
        text(
            """
            SELECT
                c.relname AS table_name,
                a.attname AS column_name,
                pg_catalog.format_type(a.atttypid, a.atttypmod) AS formatted_type
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n
              ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute AS a
              ON a.attrelid = c.oid
             AND a.attname = 'embedding'
             AND a.attnum > 0
             AND NOT a.attisdropped
            WHERE n.nspname = :schema
              AND c.relkind = 'r'
              AND c.relname IN ('chunks', 'entities', 'relations', 'summaries')
            """
        ),
        {"schema": schema},
    )
    return {(str(row.table_name), str(row.column_name)): str(row.formatted_type) for row in result}


async def exercise_minimal_b2_schema(database_url: DisposableDatabaseUrl) -> None:
    dataset_id = uuid4()
    source_id = uuid4()
    document_id = uuid4()
    chunk_a_id = uuid4()
    chunk_b_id = uuid4()
    entity_id = uuid4()
    mention_id = uuid4()
    relation_id = uuid4()
    summary_id = uuid4()
    memory_entry_id = uuid4()
    query_id = uuid4()
    feedback_id = uuid4()
    pipeline_run_id = uuid4()
    pipeline_step_id = uuid4()
    token = uuid4().hex
    vector_a = vector_literal(EXPECTED_EMBEDDING_DIMENSIONS, hot_index=0)
    vector_b = vector_literal(EXPECTED_EMBEDDING_DIMENSIONS, hot_index=1)

    engine = create_async_engine(database_url.get_secret_value(), pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            await insert_minimal_records(
                connection,
                ids={
                    "dataset_id": dataset_id,
                    "source_id": source_id,
                    "document_id": document_id,
                    "chunk_a_id": chunk_a_id,
                    "chunk_b_id": chunk_b_id,
                    "entity_id": entity_id,
                    "mention_id": mention_id,
                    "relation_id": relation_id,
                    "summary_id": summary_id,
                    "memory_entry_id": memory_entry_id,
                    "query_id": query_id,
                    "feedback_id": feedback_id,
                    "pipeline_run_id": pipeline_run_id,
                    "pipeline_step_id": pipeline_step_id,
                },
                token=token,
                vector_a=vector_a,
                vector_b=vector_b,
            )
            await assert_vector_distance_orders_expected_chunk(
                connection,
                chunk_a_id=chunk_a_id,
                query_vector=vector_a,
            )
            await assert_lexical_search_finds_expected_chunk(
                connection,
                chunk_a_id=chunk_a_id,
            )

        await assert_constraint_rejects(
            engine,
            invalid_feedback_score_sql(query_id=query_id),
            {"feedback_id": uuid4()},
        )
        await assert_constraint_rejects(
            engine,
            negative_pipeline_attempt_sql(dataset_id=dataset_id, source_id=source_id),
            {
                "run_id": uuid4(),
                "idempotency_key": f"gate-negative-attempt-{token}",
            },
        )
        await assert_constraint_rejects(
            engine,
            duplicate_idempotency_key_sql(dataset_id=dataset_id, source_id=source_id),
            {
                "run_id": uuid4(),
                "idempotency_key": f"gate-idempotency-{token}",
            },
        )
        await assert_constraint_rejects(
            engine,
            invalid_source_hash_sql(dataset_id=dataset_id),
            {"source_id": uuid4()},
        )
        await assert_constraint_rejects(
            engine,
            wrong_vector_dimension_sql(
                dataset_id=dataset_id,
                document_id=document_id,
                source_id=source_id,
            ),
            {
                "chunk_id": uuid4(),
                "wrong_embedding": vector_literal(1536),
            },
        )
    finally:
        await engine.dispose()


async def insert_minimal_records(
    connection: AsyncConnection,
    *,
    ids: Mapping[str, object],
    token: str,
    vector_a: str,
    vector_b: str,
) -> None:
    hash_a = "a" * 64
    hash_b = "b" * 64
    hash_c = "c" * 64
    await connection.execute(
        text(
            """
            INSERT INTO datasets (id, name, slug, description, status, active_generation)
            VALUES (:dataset_id, :name, :slug, NULL, 'active', 0)
            """
        ),
        {
            "dataset_id": ids["dataset_id"],
            "name": f"SM-215 Gate {token}",
            "slug": f"sm-215-gate-{token}",
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO sources (
                id, dataset_id, kind, name, mime_type, original_uri, storage_uri,
                content_sha256, normalized_sha256, byte_size, metadata, status, version
            )
            VALUES (
                :source_id, :dataset_id, 'text', 'Gate source', 'text/plain', NULL, NULL,
                :content_sha256, NULL, 42, '{}'::jsonb, 'active', 1
            )
            """
        ),
        {
            "source_id": ids["source_id"],
            "dataset_id": ids["dataset_id"],
            "content_sha256": hash_a,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO documents (
                id, dataset_id, source_id, generation, title, language, normalized_text,
                text_sha256, token_count, metadata, is_active
            )
            VALUES (
                :document_id, :dataset_id, :source_id, 0, 'Gate document', 'en',
                'sofia memory migration gate text', :text_sha256, 5, '{}'::jsonb, TRUE
            )
            """
        ),
        {
            "document_id": ids["document_id"],
            "dataset_id": ids["dataset_id"],
            "source_id": ids["source_id"],
            "text_sha256": hash_b,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO chunks (
                id, dataset_id, document_id, source_id, generation, ordinal, text,
                content_sha256, token_count, start_char, end_char, section_path, metadata,
                embedding, lexical, is_active
            )
            VALUES
            (
                :chunk_a_id, :dataset_id, :document_id, :source_id, 0, 0,
                'sofia migration gate lexical term', :chunk_a_hash, 5, 0, 35,
                ARRAY['root']::text[], '{}'::jsonb, CAST(:vector_a AS vector),
                to_tsvector('simple', 'sofia migration gate lexical term'), TRUE
            ),
            (
                :chunk_b_id, :dataset_id, :document_id, :source_id, 0, 1,
                'other migration gate text', :chunk_b_hash, 4, 36, 61,
                ARRAY['root']::text[], '{}'::jsonb, CAST(:vector_b AS vector),
                to_tsvector('simple', 'other migration gate text'), TRUE
            )
            """
        ),
        {
            "chunk_a_id": ids["chunk_a_id"],
            "chunk_b_id": ids["chunk_b_id"],
            "dataset_id": ids["dataset_id"],
            "document_id": ids["document_id"],
            "source_id": ids["source_id"],
            "chunk_a_hash": hash_c,
            "chunk_b_hash": "d" * 64,
            "vector_a": vector_a,
            "vector_b": vector_b,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO entities (
                id, dataset_id, generation, canonical_key, name, entity_type,
                description, aliases, properties, confidence, importance_weight,
                embedding, is_active
            )
            VALUES (
                :entity_id, :dataset_id, 0, :canonical_key, 'Sofia', 'person',
                'Test entity', ARRAY['Sofia']::text[], '{}'::jsonb, 0.9, 1.0,
                CAST(:vector_a AS vector), TRUE
            )
            """
        ),
        {
            "entity_id": ids["entity_id"],
            "dataset_id": ids["dataset_id"],
            "canonical_key": f"sofia-{token}",
            "vector_a": vector_a,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO entity_mentions (
                id, entity_id, chunk_id, surface_text, start_char, end_char, confidence
            )
            VALUES (:mention_id, :entity_id, :chunk_a_id, 'Sofia', 0, 5, 0.95)
            """
        ),
        {
            "mention_id": ids["mention_id"],
            "entity_id": ids["entity_id"],
            "chunk_a_id": ids["chunk_a_id"],
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO relations (
                id, dataset_id, generation, source_entity_id, target_entity_id,
                predicate, description, properties, confidence, importance_weight,
                embedding, is_active
            )
            VALUES (
                :relation_id, :dataset_id, 0, :entity_id, :entity_id, 'mentions',
                'Self relation for schema gate', '{}'::jsonb, 0.8, 1.0,
                CAST(:vector_a AS vector), TRUE
            )
            """
        ),
        {
            "relation_id": ids["relation_id"],
            "dataset_id": ids["dataset_id"],
            "entity_id": ids["entity_id"],
            "vector_a": vector_a,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO relation_evidence (relation_id, chunk_id, quote, confidence)
            VALUES (:relation_id, :chunk_a_id, 'Sofia migration gate lexical term', 0.9)
            """
        ),
        {"relation_id": ids["relation_id"], "chunk_a_id": ids["chunk_a_id"]},
    )
    await connection.execute(
        text(
            """
            INSERT INTO summaries (
                id, dataset_id, generation, target_type, target_id, level, text,
                embedding, is_active
            )
            VALUES (
                :summary_id, :dataset_id, 0, 'dataset', :dataset_id, 0,
                'Gate summary', CAST(:vector_a AS vector), TRUE
            )
            """
        ),
        {
            "summary_id": ids["summary_id"],
            "dataset_id": ids["dataset_id"],
            "vector_a": vector_a,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO memory_entries (
                id, dataset_id, source_id, session_id, entry_type, content, metadata
            )
            VALUES (
                :memory_entry_id, :dataset_id, :source_id, NULL, 'note',
                'Gate memory entry', '{}'::jsonb
            )
            """
        ),
        {
            "memory_entry_id": ids["memory_entry_id"],
            "dataset_id": ids["dataset_id"],
            "source_id": ids["source_id"],
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO queries (
                id, query_text, dataset_ids, mode, answer, "references", timings, model
            )
            VALUES (
                :query_id, 'What is Sofia?', ARRAY[:dataset_id]::uuid[], 'chunks',
                'Sofia is present in the gate data.', '[]'::jsonb, '{}'::jsonb, NULL
            )
            """
        ),
        {"query_id": ids["query_id"], "dataset_id": ids["dataset_id"]},
    )
    await connection.execute(
        text(
            """
            INSERT INTO feedback (
                id, query_id, target_type, target_id, score, comment, applied_at
            )
            VALUES (:feedback_id, :query_id, 'chunk', :chunk_a_id, 1, NULL, NULL)
            """
        ),
        {
            "feedback_id": ids["feedback_id"],
            "query_id": ids["query_id"],
            "chunk_a_id": ids["chunk_a_id"],
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO pipeline_runs (
                id, pipeline_type, dataset_id, source_id, status, idempotency_key,
                payload_hash, input, progress, current_step, attempt, worker_id,
                heartbeat_at, config_fingerprint, error_code, error_message, metrics,
                started_at, finished_at
            )
            VALUES (
                :pipeline_run_id, 'remember', :dataset_id, :source_id, 'queued',
                :idempotency_key, :payload_hash, '{}'::jsonb, 0.0, NULL, 0, NULL,
                NULL, :config_fingerprint, NULL, NULL, '{}'::jsonb, NULL, NULL
            )
            """
        ),
        {
            "pipeline_run_id": ids["pipeline_run_id"],
            "dataset_id": ids["dataset_id"],
            "source_id": ids["source_id"],
            "idempotency_key": f"gate-idempotency-{token}",
            "payload_hash": "e" * 64,
            "config_fingerprint": "f" * 64,
        },
    )
    await connection.execute(
        text(
            """
            INSERT INTO pipeline_steps (
                id, run_id, name, ordinal, status, attempt, input_hash, output,
                metrics, error, started_at, finished_at
            )
            VALUES (
                :pipeline_step_id, :pipeline_run_id, 'validate', 0, 'queued', 0,
                NULL, '{}'::jsonb, '{}'::jsonb, NULL, NULL, NULL
            )
            """
        ),
        {
            "pipeline_step_id": ids["pipeline_step_id"],
            "pipeline_run_id": ids["pipeline_run_id"],
        },
    )
    graph_result = await connection.execute(
        text(
            """
            INSERT INTO graph_outbox (
                dataset_id, aggregate_type, aggregate_id, operation, payload, status, attempt
            )
            VALUES (
                :dataset_id, 'dataset', :dataset_id, 'upsert', '{}'::jsonb, 'pending', 0
            )
            RETURNING id
            """
        ),
        {"dataset_id": ids["dataset_id"]},
    )
    assert int(graph_result.scalar_one()) > 0


async def assert_vector_distance_orders_expected_chunk(
    connection: AsyncConnection,
    *,
    chunk_a_id: object,
    query_vector: str,
) -> None:
    result = await connection.execute(
        text(
            """
            SELECT id
            FROM chunks
            ORDER BY embedding <=> CAST(:query_vector AS vector)
            LIMIT 1
            """
        ),
        {"query_vector": query_vector},
    )
    assert result.scalar_one() == chunk_a_id


async def assert_lexical_search_finds_expected_chunk(
    connection: AsyncConnection,
    *,
    chunk_a_id: object,
) -> None:
    found_result = await connection.execute(
        text(
            """
            SELECT id
            FROM chunks
            WHERE lexical @@ plainto_tsquery('simple', 'sofia')
            """
        )
    )
    assert chunk_a_id in {row.id for row in found_result}

    missing_result = await connection.execute(
        text(
            """
            SELECT id
            FROM chunks
            WHERE lexical @@ plainto_tsquery('simple', 'nonexistentterm')
            """
        )
    )
    assert chunk_a_id not in {row.id for row in missing_result}


async def assert_constraint_rejects(
    engine: object,
    sql: str,
    parameters: Mapping[str, object],
) -> None:
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            with pytest.raises(SQLAlchemyError):
                await connection.execute(text(sql), parameters)
        finally:
            await transaction.rollback()


def invalid_feedback_score_sql(*, query_id: object) -> str:
    return f"""
        INSERT INTO feedback (
            id, query_id, target_type, target_id, score, comment, applied_at
        )
        VALUES (:feedback_id, '{query_id}'::uuid, 'chunk', NULL, 2, NULL, NULL)
        """


def negative_pipeline_attempt_sql(*, dataset_id: object, source_id: object) -> str:
    return f"""
        INSERT INTO pipeline_runs (
            id, pipeline_type, dataset_id, source_id, status, idempotency_key,
            payload_hash, input, progress, current_step, attempt, worker_id,
            heartbeat_at, config_fingerprint, error_code, error_message, metrics,
            started_at, finished_at
        )
        VALUES (
            :run_id, 'remember', '{dataset_id}'::uuid, '{source_id}'::uuid, 'queued',
            :idempotency_key, '{"a" * 64}', '{{}}'::jsonb, 0.0, NULL, -1, NULL, NULL,
            '{"b" * 64}', NULL, NULL, '{{}}'::jsonb, NULL, NULL
        )
        """


def duplicate_idempotency_key_sql(*, dataset_id: object, source_id: object) -> str:
    return f"""
        INSERT INTO pipeline_runs (
            id, pipeline_type, dataset_id, source_id, status, idempotency_key,
            payload_hash, input, progress, current_step, attempt, worker_id,
            heartbeat_at, config_fingerprint, error_code, error_message, metrics,
            started_at, finished_at
        )
        VALUES (
            :run_id, 'remember', '{dataset_id}'::uuid, '{source_id}'::uuid, 'queued',
            :idempotency_key, '{"c" * 64}', '{{}}'::jsonb, 0.0, NULL, 0, NULL, NULL,
            '{"d" * 64}', NULL, NULL, '{{}}'::jsonb, NULL, NULL
        )
        """


def invalid_source_hash_sql(*, dataset_id: object) -> str:
    return f"""
        INSERT INTO sources (
            id, dataset_id, kind, name, mime_type, original_uri, storage_uri,
            content_sha256, normalized_sha256, byte_size, metadata, status, version
        )
        VALUES (
            :source_id, '{dataset_id}'::uuid, 'text', 'Invalid hash source', 'text/plain',
            NULL, NULL, 'not-a-valid-sha256', NULL, 1, '{{}}'::jsonb, 'active', 99
        )
        """


def wrong_vector_dimension_sql(
    *,
    dataset_id: object,
    document_id: object,
    source_id: object,
) -> str:
    return f"""
        INSERT INTO chunks (
            id, dataset_id, document_id, source_id, generation, ordinal, text,
            content_sha256, token_count, start_char, end_char, section_path, metadata,
            embedding, lexical, is_active
        )
        VALUES (
            :chunk_id, '{dataset_id}'::uuid, '{document_id}'::uuid, '{source_id}'::uuid,
            0, 99, 'wrong vector dimension', '{"e" * 64}', 3, 0, 22,
            ARRAY['root']::text[], '{{}}'::jsonb, CAST(:wrong_embedding AS vector),
            to_tsvector('simple', 'wrong vector dimension'), TRUE
        )
        """


def test_postgres_migration_gate_uses_canonical_compose_pgvector_image() -> None:
    assert compose_postgres_image() == "pgvector/pgvector:0.8.1-pg17"


def test_vector_literal_builds_exact_dimension_without_secrets() -> None:
    assert vector_literal(4, hot_index=2) == "[0,0,1,0]"


def test_postgres_gate_backend_selects_local_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(POSTGRES_MIGRATION_GATE_BACKEND_ENV, POSTGRES_GATE_BACKEND_LOCAL)

    assert selected_postgres_gate_backend() == POSTGRES_GATE_BACKEND_LOCAL


def test_postgres_gate_backend_selects_testcontainers_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(POSTGRES_MIGRATION_GATE_BACKEND_ENV, POSTGRES_GATE_BACKEND_TESTCONTAINERS)

    assert selected_postgres_gate_backend() == POSTGRES_GATE_BACKEND_TESTCONTAINERS


def test_postgres_gate_backend_rejects_missing_or_invalid_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(POSTGRES_MIGRATION_GATE_BACKEND_ENV, raising=False)
    with pytest.raises(AssertionError, match=POSTGRES_MIGRATION_GATE_BACKEND_ENV):
        selected_postgres_gate_backend()

    monkeypatch.setenv(POSTGRES_MIGRATION_GATE_BACKEND_ENV, "automatic")
    with pytest.raises(AssertionError, match="local, testcontainers"):
        selected_postgres_gate_backend()


def test_generated_temporary_database_name_has_safe_prefix() -> None:
    temporary_database_name = generate_temporary_database_name()

    assert temporary_database_name.startswith(TEMPORARY_DATABASE_PREFIX)
    assert TEMPORARY_DATABASE_PATTERN.fullmatch(temporary_database_name) is not None


def test_temporary_database_name_cannot_equal_configured_database() -> None:
    temporary_database_name = generate_temporary_database_name()

    with pytest.raises(AssertionError, match="must differ"):
        assert_safe_temporary_database_name(
            temporary_database_name,
            configured_database_name=temporary_database_name,
        )


def test_drop_database_guard_rejects_names_outside_gate_prefix() -> None:
    with pytest.raises(AssertionError, match="gate-safe prefix"):
        assert_safe_drop_database_name("cognee_db")


def test_build_temporary_database_url_only_replaces_database_name() -> None:
    original_url = (
        "postgresql+asyncpg://gate_user:example-password@db.example.test:5432/"
        "configured_db?ssl=require"
    )
    temporary_database_name = generate_temporary_database_name()

    temporary_url = build_temporary_database_url(original_url, temporary_database_name)
    original_parts = urlsplit(original_url)
    temporary_parts = urlsplit(temporary_url)

    assert temporary_parts.scheme == original_parts.scheme
    assert temporary_parts.netloc == original_parts.netloc
    assert temporary_parts.query == original_parts.query
    assert temporary_parts.fragment == original_parts.fragment
    assert configured_database_name(temporary_url) == temporary_database_name
    assert configured_database_name(temporary_url) != configured_database_name(original_url)


def test_temporary_database_url_safety_error_does_not_expose_password() -> None:
    original_database = f"{TEMPORARY_DATABASE_PREFIX}{'a' * 32}"
    original_url = (
        f"postgresql+asyncpg://gate_user:example-password@db.example.test/{original_database}"
    )

    with pytest.raises(AssertionError) as excinfo:
        build_temporary_database_url(original_url, original_database)

    assert "example-password" not in str(excinfo.value)


def test_disposable_database_url_repr_and_str_are_redacted() -> None:
    database_url = "postgresql+asyncpg://gate_user:example-password@db.example.test/gate_db"
    disposable_database_url = DisposableDatabaseUrl(database_url)

    assert disposable_database_url.get_secret_value() == database_url
    assert "example-password" not in repr(disposable_database_url)
    assert "example-password" not in str(disposable_database_url)
    assert "postgresql+asyncpg://" not in repr(disposable_database_url)
    assert "postgresql+asyncpg://" not in str(disposable_database_url)


def test_postgres_fk_delete_action_mapping_uses_textual_catalog_codes() -> None:
    assert postgres_fk_delete_action_name("a") == "NO ACTION"
    assert postgres_fk_delete_action_name("r") == "RESTRICT"
    assert postgres_fk_delete_action_name("c") == "CASCADE"
    assert postgres_fk_delete_action_name("n") == "SET NULL"
    assert postgres_fk_delete_action_name("d") == "SET DEFAULT"
    with pytest.raises(KeyError):
        postgres_fk_delete_action_name("b'r'")


def test_postgres_constraint_type_mapping_uses_textual_catalog_codes() -> None:
    assert postgres_constraint_type_code("p") == "p"
    assert postgres_constraint_type_code("f") == "f"
    assert postgres_constraint_type_code("u") == "u"
    assert postgres_constraint_type_code("c") == "c"
    with pytest.raises(KeyError):
        postgres_constraint_type_code("b'p'")


def test_hnsw_halfvec_index_accepts_postgresql_parenthesized_cast_definition() -> None:
    assert_hnsw_halfvec_index(
        "CREATE INDEX ix_chunks_embedding_halfvec_hnsw "
        "ON public.chunks USING hnsw "
        "(((embedding)::halfvec(3072)) halfvec_cosine_ops)"
    )


def test_schema_guard_policy_reused_by_migration_gate() -> None:
    assert (
        frozenset({"users", "roles", "permissions", "acl", "api_keys", "settings", "tenants"})
        == FORBIDDEN_TABLES
    )
    assert frozenset({"owner_id", "tenant_id"}) == FORBIDDEN_COLUMNS
    assert frozenset({"vector", "pg_trgm", "citext"}) == REQUIRED_EXTENSIONS
    assert len(REQUIRED_TABLES) == 15
