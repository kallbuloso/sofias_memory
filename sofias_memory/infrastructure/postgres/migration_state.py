"""Read-only Alembic schema-migration state classification (ADR-0015, SM-901).

This module answers "what schema state is this database in?" -- it never
answers "should I execute Alembic now?" and never executes any DDL/DML
itself. Advisory-lock acquisition, spawning ``alembic upgrade head`` as a
subprocess, and integrating this classification into ``lifespan.py`` are all
out of scope here and belong to SM-902.

The nine states below are exactly the states an application image can
observe about a database using only its own packaged Alembic revision graph
plus read-only PostgreSQL catalog queries -- never a hypothetical global
ordering across every possible application version (Feature Contract v0.6.0
S4, erratum). Ancestry is always determined via Alembic's own official
``ScriptDirectory`` revision-graph API, never by parsing migration filenames,
``down_revision`` strings, or comparing revision identifiers lexically or
numerically.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.script.revision import ResolutionError
from alembic.util.exc import CommandError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection


class MigrationSchemaState(StrEnum):
    """The nine states an application image can observe about a database's
    Alembic schema revision, per Feature Contract v0.6.0 S4."""

    PRISTINE_FRESH_SCHEMA = "pristine_fresh_schema"
    UNVERSIONED_NON_EMPTY_SCHEMA = "unversioned_non_empty_schema"
    EXACT_HEAD = "exact_head"
    KNOWN_ANCESTOR = "known_ancestor"
    CODE_MULTIPLE_HEADS = "code_multiple_heads"
    VERSION_TABLE_EMPTY = "version_table_empty"
    DATABASE_MULTIPLE_REVISIONS = "database_multiple_revisions"
    KNOWN_NON_ANCESTOR = "known_non_ancestor"
    UNRECOGNIZED_REVISION = "unrecognized_revision"


@dataclass(frozen=True)
class MigrationStateSnapshot:
    """Authoritative, read-only observation of catalog + revision-graph state.

    ``alembic_version_table_present`` and ``database_revisions`` are kept as
    two separate fields specifically so "table absent" and "table present
    with zero revision rows" are never collapsed into the same empty value
    (Feature Contract S4.11) -- the former can be PRISTINE_FRESH_SCHEMA/
    UNVERSIONED_NON_EMPTY_SCHEMA, the latter is always VERSION_TABLE_EMPTY.
    """

    alembic_version_table_present: bool
    database_revisions: frozenset[str]
    application_base_tables_present: bool
    code_heads: frozenset[str]


class RevisionGraph(Protocol):
    """The minimal revision-graph queries the classifier needs, expressed as
    a narrow protocol so the pure classification logic in
    :func:`classify_migration_state` is unit-testable without a real Alembic
    ``ScriptDirectory``. The only production implementation
    (:class:`AlembicScriptDirectoryRevisionGraph`) delegates every method to
    Alembic's own official API -- this protocol is not a parallel
    reimplementation of DAG ancestry."""

    def is_known_revision(self, revision: str) -> bool:
        """Whether ``revision`` is resolvable by this image's own Alembic
        revision graph -- ``False`` covers a genuinely foreign/unrelated
        identifier and a revision produced by a newer, not-yet-seen
        application image identically; this graph cannot and does not try
        to distinguish those two realities (Feature Contract S4.9)."""

    def ancestors_of(self, head: str) -> frozenset[str]:
        """Every revision on the path from ``head`` down to the root,
        inclusive of ``head`` itself."""


class AlembicScriptDirectoryRevisionGraph:
    """:class:`RevisionGraph` backed by a real ``alembic.script.ScriptDirectory``.

    Every method call is Alembic's own official revision-graph API --
    ``get_revision()``/``iterate_revisions()`` -- never manual parsing of
    filenames, ``down_revision`` strings, or revision-ID ordering.
    """

    def __init__(self, script_directory: ScriptDirectory) -> None:
        self._script_directory = script_directory

    def is_known_revision(self, revision: str) -> bool:
        try:
            self._script_directory.get_revision(revision)
        except (CommandError, ResolutionError):
            return False
        return True

    def ancestors_of(self, head: str) -> frozenset[str]:
        return frozenset(
            revision.revision for revision in self._script_directory.iterate_revisions(head, "base")
        )


def load_revision_graph() -> RevisionGraph:
    """Build a :class:`RevisionGraph` from this project's own packaged
    ``alembic.ini`` + ``migrations/`` -- the same configuration
    ``readiness.load_code_heads()`` already resolves, kept as a small,
    deliberate local duplication rather than reaching into that module's
    private helpers across module boundaries."""

    config_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    config = Config(str(config_path))
    return AlembicScriptDirectoryRevisionGraph(ScriptDirectory.from_config(config))


def classify_migration_state(
    snapshot: MigrationStateSnapshot,
    *,
    revision_graph: RevisionGraph,
) -> MigrationSchemaState:
    """Pure classification: no I/O, no mutation, deterministic given a
    snapshot + revision graph. Precedence is deliberate and tested
    (Feature Contract S4 / backlog SM-901 S21):

    1. Multiple (or zero) application code heads is a code-side problem
       independent of database state and is checked first, so it can never
       be shadowed by a database-side classification.
    2. ``alembic_version`` absence is classified before any attempt to
       resolve a revision against the graph -- there is no revision to
       resolve in that case.
    3. Only once exactly one code head and exactly one database revision
       are established does ancestry resolution run, always via the
       official Alembic revision-graph API.
    """

    if len(snapshot.code_heads) != 1:
        return MigrationSchemaState.CODE_MULTIPLE_HEADS

    if not snapshot.alembic_version_table_present:
        if snapshot.application_base_tables_present:
            return MigrationSchemaState.UNVERSIONED_NON_EMPTY_SCHEMA
        return MigrationSchemaState.PRISTINE_FRESH_SCHEMA

    if len(snapshot.database_revisions) == 0:
        return MigrationSchemaState.VERSION_TABLE_EMPTY
    if len(snapshot.database_revisions) > 1:
        return MigrationSchemaState.DATABASE_MULTIPLE_REVISIONS

    (database_revision,) = snapshot.database_revisions
    (code_head,) = snapshot.code_heads

    if database_revision == code_head:
        return MigrationSchemaState.EXACT_HEAD

    if database_revision in revision_graph.ancestors_of(code_head):
        return MigrationSchemaState.KNOWN_ANCESTOR

    if revision_graph.is_known_revision(database_revision):
        return MigrationSchemaState.KNOWN_NON_ANCESTOR

    return MigrationSchemaState.UNRECOGNIZED_REVISION


async def collect_migration_state_snapshot(
    connection: AsyncConnection,
    *,
    code_heads: frozenset[str],
) -> MigrationStateSnapshot:
    """Read-only observation of the current database's Alembic bookkeeping
    state. Every statement issued here is a ``SELECT`` against
    ``information_schema``/the ``alembic_version`` table -- no ``CREATE``,
    ``ALTER``, ``DROP``, ``INSERT``, ``UPDATE``, ``DELETE``, or ``TRUNCATE``
    is ever executed by this function."""

    schema = await _current_schema(connection)
    alembic_version_table_present = await _alembic_version_table_present(connection, schema=schema)
    database_revisions = (
        await _database_revisions(connection) if alembic_version_table_present else frozenset[str]()
    )
    application_base_tables_present = await _application_base_tables_present(
        connection, schema=schema
    )
    return MigrationStateSnapshot(
        alembic_version_table_present=alembic_version_table_present,
        database_revisions=database_revisions,
        application_base_tables_present=application_base_tables_present,
        code_heads=code_heads,
    )


async def _current_schema(connection: AsyncConnection) -> str:
    result = await connection.execute(text("SELECT current_schema()"))
    return str(result.scalar_one())


async def _alembic_version_table_present(connection: AsyncConnection, *, schema: str) -> bool:
    result = await connection.execute(
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
    return result.first() is not None


async def _database_revisions(connection: AsyncConnection) -> frozenset[str]:
    result = await connection.execute(text("SELECT version_num FROM alembic_version"))
    return frozenset(str(row["version_num"]) for row in result.mappings())


async def _application_base_tables_present(connection: AsyncConnection, *, schema: str) -> bool:
    result = await connection.execute(
        text(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = :schema
              AND table_type = 'BASE TABLE'
              AND table_name <> 'alembic_version'
            LIMIT 1
            """
        ),
        {"schema": schema},
    )
    return result.first() is not None
