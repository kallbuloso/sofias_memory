"""Read-only PostgreSQL readiness checks."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from sofias_memory.config import Settings
from sofias_memory.infrastructure.postgres.engine import (
    create_async_engine_from_settings,
    dispose_async_engine,
)
from sofias_memory.observability.logging import get_logger

POSTGRES_NOT_READY_DETAIL = "postgres not ready"
REQUIRED_EXTENSIONS = frozenset({"vector", "pg_trgm", "citext"})
EMBEDDING_COLUMNS = (
    ("chunks", "embedding"),
    ("entities", "embedding"),
    ("relations", "embedding"),
    ("summaries", "embedding"),
)
VECTOR_TYPE_PATTERN = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_$]*\.)?vector\((\d+)\)$")

EngineFactory = Callable[[Settings], AsyncEngine]
CodeHeadsLoader = Callable[[], frozenset[str]]


@dataclass(frozen=True)
class PostgresReadinessSnapshot:
    """PostgreSQL catalog state needed by the readiness evaluator."""

    code_heads: frozenset[str]
    database_revisions: frozenset[str]
    installed_extensions: frozenset[str]
    embedding_column_types: Mapping[tuple[str, str], str | None]


@dataclass(frozen=True)
class PostgresReadinessResult:
    """Internal readiness result; failures are intentionally not public details."""

    ready: bool
    failures: tuple[str, ...] = ()


class PostgresReadinessChecker:
    """Read-only PostgreSQL readiness checker owned by one application instance."""

    def __init__(
        self,
        settings: Settings,
        *,
        engine_factory: EngineFactory = create_async_engine_from_settings,
        code_heads_loader: CodeHeadsLoader | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine_factory(settings)
        self._code_heads_loader = code_heads_loader or load_code_heads

    async def check(self) -> PostgresReadinessResult:
        try:
            code_heads = self._code_heads_loader()
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
                schema = await _current_schema(connection)
                snapshot = PostgresReadinessSnapshot(
                    code_heads=code_heads,
                    database_revisions=await _database_revisions(connection, schema=schema),
                    installed_extensions=await _installed_extensions(connection),
                    embedding_column_types=await _embedding_column_types(
                        connection,
                        schema=schema,
                    ),
                )
        except SQLAlchemyError as exc:
            _log_postgres_readiness_failure(type(exc).__name__)
            return PostgresReadinessResult(ready=False, failures=("connection",))
        except OSError as exc:
            _log_postgres_readiness_failure(type(exc).__name__)
            return PostgresReadinessResult(ready=False, failures=("connection",))

        return evaluate_postgres_readiness(
            snapshot,
            expected_embedding_dimensions=self._settings.embedding_dimensions,
        )

    async def dispose(self) -> None:
        await dispose_async_engine(self._engine)


def load_code_heads() -> frozenset[str]:
    config_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    config = Config(str(config_path))
    script = ScriptDirectory.from_config(config)
    return frozenset(script.get_heads())


def evaluate_postgres_readiness(
    snapshot: PostgresReadinessSnapshot,
    *,
    expected_embedding_dimensions: int,
) -> PostgresReadinessResult:
    failures: list[str] = []

    if len(snapshot.code_heads) != 1:
        failures.append("code_heads")
    if len(snapshot.database_revisions) != 1:
        failures.append("database_revisions")
    if snapshot.database_revisions != snapshot.code_heads:
        failures.append("revision_mismatch")

    missing_extensions = REQUIRED_EXTENSIONS - snapshot.installed_extensions
    if missing_extensions:
        failures.append("extensions")

    for table_name, column_name in EMBEDDING_COLUMNS:
        formatted_type = snapshot.embedding_column_types.get((table_name, column_name))
        if formatted_type is None:
            failures.append(f"{table_name}.{column_name}")
            continue
        if not embedding_type_matches_dimension(
            formatted_type,
            expected_embedding_dimensions=expected_embedding_dimensions,
        ):
            failures.append(f"{table_name}.{column_name}")

    return PostgresReadinessResult(ready=not failures, failures=tuple(failures))


def embedding_type_matches_dimension(
    formatted_type: str,
    *,
    expected_embedding_dimensions: int,
) -> bool:
    match = VECTOR_TYPE_PATTERN.fullmatch(formatted_type.strip().lower())
    if match is None:
        return False
    return int(match.group(1)) == expected_embedding_dimensions


async def _current_schema(connection: AsyncConnection) -> str:
    result = await connection.execute(text("SELECT current_schema()"))
    return str(result.scalar_one())


async def _database_revisions(connection: AsyncConnection, *, schema: str) -> frozenset[str]:
    table_exists_result = await connection.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = :schema
              AND table_name = 'alembic_version'
              AND table_type = 'BASE TABLE'
            """
        ),
        {"schema": schema},
    )
    if table_exists_result.first() is None:
        return frozenset()

    revision_result = await connection.execute(text("SELECT version_num FROM alembic_version"))
    return frozenset(str(row["version_num"]) for row in revision_result.mappings())


async def _installed_extensions(connection: AsyncConnection) -> frozenset[str]:
    result = await connection.execute(
        text(
            """
            SELECT extname
            FROM pg_catalog.pg_extension
            WHERE extname IN ('vector', 'pg_trgm', 'citext')
            """
        )
    )
    return frozenset(str(row["extname"]) for row in result.mappings())


async def _embedding_column_types(
    connection: AsyncConnection,
    *,
    schema: str,
) -> dict[tuple[str, str], str | None]:
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
            LEFT JOIN pg_catalog.pg_attribute AS a
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
    column_types: dict[tuple[str, str], str | None] = {}
    for row in result.mappings():
        mapping = row
        column_types[
            (
                _required_string(mapping, "table_name"),
                _optional_string(mapping, "column_name") or "embedding",
            )
        ] = _optional_string(mapping, "formatted_type")
    return column_types


def _required_string(mapping: RowMapping, key: str) -> str:
    return str(mapping[key])


def _optional_string(mapping: RowMapping, key: str) -> str | None:
    value = mapping[key]
    if value is None:
        return None
    return str(value)


def _log_postgres_readiness_failure(exception_type: str) -> None:
    get_logger(__name__).warning(
        "postgres_readiness_check_failed",
        exception_type=exception_type,
    )
