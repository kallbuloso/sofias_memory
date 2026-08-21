from __future__ import annotations

from typing import cast

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

from sofias_memory.config import API_KEY_PREFIX, Settings
from sofias_memory.infrastructure.postgres.readiness import (
    EMBEDDING_COLUMNS,
    REQUIRED_EXTENSIONS,
    PostgresReadinessChecker,
    PostgresReadinessSnapshot,
    embedding_type_matches_dimension,
    evaluate_postgres_readiness,
    load_code_heads,
)

VALID_API_KEY = f"{API_KEY_PREFIX}{'a' * 32}"
VALID_DATABASE_URL = "postgresql+asyncpg://sofias_memory:db-secret@postgres:5432/db"
VALID_NEO4J_PASSWORD = "fake-neo4j-password"
VALID_LLM_API_KEY = "sk-fake-test-key"


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": VALID_API_KEY,
        "database_url": VALID_DATABASE_URL,
        "neo4j_password": VALID_NEO4J_PASSWORD,
        "llm_api_key": VALID_LLM_API_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def healthy_snapshot(**overrides: object) -> PostgresReadinessSnapshot:
    values: dict[str, object] = {
        "code_heads": frozenset({"0007"}),
        "database_revisions": frozenset({"0007"}),
        "installed_extensions": REQUIRED_EXTENSIONS,
        "embedding_column_types": {column: "vector(3072)" for column in EMBEDDING_COLUMNS},
    }
    values.update(overrides)
    return PostgresReadinessSnapshot(**values)  # type: ignore[arg-type]


def test_healthy_postgres_readiness_snapshot_is_ready() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is True
    assert result.failures == ()


def test_alembic_revision_mismatch_is_not_ready() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(database_revisions=frozenset({"0006"})),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "revision_mismatch" in result.failures


def test_missing_alembic_revision_is_not_ready() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(database_revisions=frozenset()),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "database_revisions" in result.failures


def test_multiple_code_heads_are_not_ready() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(code_heads=frozenset({"0007", "branch"})),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "code_heads" in result.failures


@pytest.mark.parametrize("missing_extension", sorted(REQUIRED_EXTENSIONS))
def test_missing_required_extension_is_not_ready(missing_extension: str) -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(installed_extensions=REQUIRED_EXTENSIONS - {missing_extension}),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "extensions" in result.failures


def test_wrong_embedding_dimension_is_not_ready() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(
            embedding_column_types={
                column: ("vector(1536)" if column == ("chunks", "embedding") else "vector(3072)")
                for column in EMBEDDING_COLUMNS
            }
        ),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "chunks.embedding" in result.failures


@pytest.mark.parametrize("formatted_type", ["double precision[]", "text", "halfvec(3072)"])
def test_wrong_embedding_type_is_not_ready(formatted_type: str) -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(
            embedding_column_types={
                column: (formatted_type if column == ("entities", "embedding") else "vector(3072)")
                for column in EMBEDDING_COLUMNS
            }
        ),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "entities.embedding" in result.failures


def test_missing_embedding_column_is_not_ready_when_table_exists() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(
            embedding_column_types={
                column: ("vector(3072)" if column != ("relations", "embedding") else None)
                for column in EMBEDDING_COLUMNS
            }
        ),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "relations.embedding" in result.failures


def test_missing_embedding_table_is_not_ready_at_current_head() -> None:
    result = evaluate_postgres_readiness(
        healthy_snapshot(
            embedding_column_types={
                column: "vector(3072)"
                for column in EMBEDDING_COLUMNS
                if column != ("summaries", "embedding")
            }
        ),
        expected_embedding_dimensions=3072,
    )

    assert result.ready is False
    assert "summaries.embedding" in result.failures


def test_embedding_type_accepts_expected_vector_dimension() -> None:
    assert embedding_type_matches_dimension(
        "vector(3072)",
        expected_embedding_dimensions=3072,
    )


def test_embedding_type_rejects_wrong_vector_dimension() -> None:
    assert not embedding_type_matches_dimension(
        "vector(1536)",
        expected_embedding_dimensions=3072,
    )


def test_code_heads_loader_reads_current_alembic_head_without_shelling_out() -> None:
    assert load_code_heads() == frozenset({"0008"})


class FakeResult:
    def __init__(
        self,
        *,
        scalar: str | None = None,
        rows: tuple[dict[str, object], ...] = (),
    ) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one(self) -> str:
        if self._scalar is None:
            raise AssertionError("scalar result not configured")
        return self._scalar

    def first(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def mappings(self) -> tuple[dict[str, object], ...]:
        return self._rows


class FakeConnection:
    def __init__(self) -> None:
        self.executed_sql: list[str] = []

    async def execute(self, statement: object, params: object | None = None) -> FakeResult:
        sql = str(statement)
        self.executed_sql.append(sql)
        if "SELECT 1" in sql:
            return FakeResult()
        if "SELECT current_schema()" in sql:
            return FakeResult(scalar="public")
        if "information_schema.tables" in sql:
            return FakeResult(rows=({"table_name": "alembic_version"},))
        if "SELECT version_num FROM alembic_version" in sql:
            return FakeResult(rows=({"version_num": "0007"},))
        if "pg_catalog.pg_extension" in sql:
            return FakeResult(
                rows=tuple({"extname": extension} for extension in REQUIRED_EXTENSIONS)
            )
        if "pg_catalog.pg_class" in sql:
            return FakeResult(
                rows=tuple(
                    {
                        "table_name": table_name,
                        "column_name": column_name,
                        "formatted_type": "vector(3072)",
                    }
                    for table_name, column_name in EMBEDDING_COLUMNS
                )
            )
        raise AssertionError(f"unexpected SQL: {sql}")


class FakeConnectionContext:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def __aenter__(self) -> FakeConnection:
        return self._connection

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None


class FakeEngine:
    def __init__(self, connection: FakeConnection) -> None:
        self.connection = connection
        self.dispose_calls = 0

    def connect(self) -> FakeConnectionContext:
        return FakeConnectionContext(self.connection)

    async def dispose(self) -> None:
        self.dispose_calls += 1


class FailingEngine:
    def connect(self) -> object:
        raise SQLAlchemyError("database unavailable")

    async def dispose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_checker_executes_lightweight_catalog_queries_and_returns_ready() -> None:
    connection = FakeConnection()
    fake_engine = FakeEngine(connection)
    checker = PostgresReadinessChecker(
        make_settings(),
        engine_factory=lambda settings: cast(AsyncEngine, fake_engine),
        code_heads_loader=lambda: frozenset({"0007"}),
    )

    result = await checker.check()
    await checker.dispose()

    assert result.ready is True
    assert any("SELECT 1" in sql for sql in connection.executed_sql)
    assert any("SELECT current_schema()" in sql for sql in connection.executed_sql)
    assert any("alembic_version" in sql for sql in connection.executed_sql)
    assert any("pg_catalog.pg_extension" in sql for sql in connection.executed_sql)
    assert any("pg_catalog.format_type" in sql for sql in connection.executed_sql)
    assert not any("CREATE " in sql.upper() for sql in connection.executed_sql)
    assert not any("INSERT " in sql.upper() for sql in connection.executed_sql)
    assert fake_engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_checker_returns_not_ready_when_database_is_unavailable() -> None:
    checker = PostgresReadinessChecker(
        make_settings(),
        engine_factory=lambda settings: cast(AsyncEngine, FailingEngine()),
        code_heads_loader=lambda: frozenset({"0007"}),
    )

    result = await checker.check()

    assert result.ready is False
    assert result.failures == ("connection",)
