"""Real-PostgreSQL 9-states x 2-modes hardening matrix (backlog SM-905,
ADR-0015 / Feature Contract v0.6.0 SS4).

SM-901..SM-904 already proved, piecemeal, that classification is correct
(unit, fakes) and that the two migrate-eligible states/lock/failure/shutdown/
orphan-safety properties hold against real PostgreSQL
(``test_migration_bootstrap_postgres_integration.py``,
``test_migration_bootstrap_supervisor_loss_postgres_integration.py``). This
module closes the one remaining gap ADR-0015's own "Testing obligations"
section names but no earlier ticket exercised end-to-end: **every one of the
nine observable schema states, under both ``auto`` and ``verify_only``,
against a real PostgreSQL database** -- with an explicit, direct proof that
the automatic Alembic subprocess is invoked exactly zero or one times, never
inferred merely from the final schema revision.

Seven of the nine states are reachable using only this project's own real,
unmodified Alembic revision graph (a genuinely empty database, a real
migration to a real ancestor/head, or a minimal, explicitly-documented
direct edit of the real ``alembic_version`` bookkeeping table -- never a
parallel reimplementation of migration application). The remaining two
(``CODE_MULTIPLE_HEADS``, ``KNOWN_NON_ANCESTOR``) are impossible to produce
from this project's own graph, which is a single linear chain with one head
-- reaching a "known revision that is not an ancestor" or "more than one
code head" requires an actual branch, which does not exist in production.
For exactly those two, a small, real, temporary ``alembic.script.ScriptDirectory``
(three real revision files forming one real fork) is constructed via
Alembic's own official API and injected through ``MigrationBootstrap``'s and
``PostgresReadinessChecker``'s existing ``code_heads_loader``/
``revision_graph_loader`` constructor seams -- PostgreSQL itself remains
real and authoritative for the other half of the observed state in both
cases.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from sofias_memory.infrastructure.postgres.migration_state import (
    AlembicScriptDirectoryRevisionGraph,
    RevisionGraph,
)
from sofias_memory.infrastructure.postgres.readiness import PostgresReadinessChecker
from sofias_memory.services.migration_bootstrap import (
    MigrationBootstrap,
    MigrationClassificationFailedError,
    MigrationSchemaInvalidError,
)
from tests.integration.test_migration_bootstrap_postgres_integration import (
    CountingMigrationRunner,
    create_arbitrary_base_table,
    dedicated_database_url,  # noqa: F401 - reused as a pytest fixture by name
    migration_bootstrap_settings,
    reset_dedicated_database,
    upgrade_in_process_to,
)
from tests.integration.test_postgres_migration_gate import (
    alembic_config,
    single_code_head,
    single_down_revision,
)

CodeHeadsLoader = Callable[[], frozenset[str]]
RevisionGraphLoader = Callable[[], RevisionGraph]


async def _execute_admin_sql(database_url: str, *statements: str) -> None:
    """Direct, explicitly-labelled bookkeeping-table manipulation -- used
    only to construct an otherwise-unreachable invalid/edge classification
    state for this test matrix, never as a substitute for a real Alembic
    migration and never exercised by production code."""

    engine = create_async_engine(database_url, pool_pre_ping=True)
    try:
        async with engine.begin() as connection:
            for statement in statements:
                await connection.execute(text(statement))
    finally:
        await engine.dispose()


def _write_fake_revision_file(
    versions_dir: Path, filename: str, revision: str, down_revision: str | None
) -> None:
    (versions_dir / f"{filename}.py").write_text(
        f'revision = "{revision}"\n'
        f"down_revision = {down_revision!r}\n"
        "branch_labels = None\n"
        "depends_on = None\n\n"
        "def upgrade():\n    pass\n\n"
        "def downgrade():\n    pass\n"
    )


@pytest.fixture(scope="module")
def branch_revision_graph() -> Iterator[tuple[RevisionGraph, frozenset[str]]]:
    """A real, temporary, two-revision-file fork (``root`` -> ``branch_a``,
    ``root`` -> ``branch_b``), read through Alembic's own official
    ``ScriptDirectory`` API -- never a hand-rolled parallel ancestry
    implementation. This project's own real revision graph is a single
    linear chain and cannot produce ``CODE_MULTIPLE_HEADS`` or
    ``KNOWN_NON_ANCESTOR`` on its own; this fixture is the smallest genuine
    Alembic graph that can."""

    with tempfile.TemporaryDirectory(prefix="sofias_memory_matrix_branch_") as tmp_dir:
        versions_dir = Path(tmp_dir) / "versions"
        versions_dir.mkdir()
        _write_fake_revision_file(versions_dir, "root", "mtx_root", None)
        _write_fake_revision_file(versions_dir, "branch_a", "mtx_branch_a", "mtx_root")
        _write_fake_revision_file(versions_dir, "branch_b", "mtx_branch_b", "mtx_root")

        config = Config()
        config.set_main_option("script_location", tmp_dir)
        script_directory = ScriptDirectory.from_config(config)
        graph: RevisionGraph = AlembicScriptDirectoryRevisionGraph(script_directory)
        yield graph, frozenset(script_directory.get_heads())


# ---------------------------------------------------------------------------
# Per-state fixture construction -- each function takes a freshly-reset
# dedicated database (public schema dropped/recreated) to exactly the target
# state, using only real PostgreSQL DDL/DML and, where unavoidable, a
# minimal, explicitly-labelled direct edit of `alembic_version`.
# ---------------------------------------------------------------------------


async def _construct_nothing(database_url: str) -> None:
    """PRISTINE_FRESH_SCHEMA (the freshly-reset database itself) and
    CODE_MULTIPLE_HEADS (database state is irrelevant -- classification
    short-circuits on code-side head count before ever reading it)."""


async def _construct_unversioned_non_empty(database_url: str) -> None:
    await create_arbitrary_base_table(database_url)


async def _construct_exact_head(database_url: str) -> None:
    await upgrade_in_process_to(database_url, single_code_head(alembic_config()))


async def _construct_known_ancestor(database_url: str) -> None:
    config = alembic_config()
    head = single_code_head(config)
    ancestor = single_down_revision(config, head)
    await upgrade_in_process_to(database_url, ancestor)


async def _construct_version_table_empty(database_url: str) -> None:
    await upgrade_in_process_to(database_url, single_code_head(alembic_config()))
    await _execute_admin_sql(database_url, "DELETE FROM alembic_version")


async def _construct_database_multiple_revisions(database_url: str) -> None:
    await upgrade_in_process_to(database_url, single_code_head(alembic_config()))
    await _execute_admin_sql(
        database_url,
        "INSERT INTO alembic_version (version_num) VALUES ('mtx_extra_revision_row')",
    )


async def _construct_unrecognized_revision(database_url: str) -> None:
    """The rollback/newer-image scenario: a single revision the running
    image's own real revision graph cannot resolve at all -- indistinguishable
    (by design, ADR-0015) from a foreign/corrupt identifier."""

    await upgrade_in_process_to(database_url, single_code_head(alembic_config()))
    await _execute_admin_sql(
        database_url,
        "UPDATE alembic_version SET version_num = 'unrecognized_future_rev'",
    )


async def _construct_known_non_ancestor(database_url: str) -> None:
    """A single revision (``mtx_branch_b``) that the ``branch_revision_graph``
    fixture's real Alembic graph resolves, but which is not an ancestor of
    that same fixture's ``mtx_branch_a`` -- a real fork, never lexical
    ordering. The ``alembic_version`` table shape below is Alembic's own
    stable bookkeeping schema, hand-created here (rather than reached via a
    real upgrade) only because no real migration in this fixture's graph
    ever produces this specific, otherwise-unreachable revision."""

    await _execute_admin_sql(
        database_url,
        "CREATE TABLE alembic_version ("
        "version_num VARCHAR(32) NOT NULL, "
        "CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num))",
        "INSERT INTO alembic_version (version_num) VALUES ('mtx_branch_b')",
    )


# ---------------------------------------------------------------------------
# Shared driver: constructs the target state twice (once per mode, from a
# freshly-reset database each time) and asserts the frozen (mode, state) ->
# outcome matrix, including the exact automatic-Alembic-invocation count.
# ---------------------------------------------------------------------------


async def _run_matrix_state(
    database_url: str,
    *,
    construct: Callable[[str], Awaitable[None]],
    expect_migrate: bool = False,
    expect_fail_closed: bool = False,
    code_heads_loader: CodeHeadsLoader | None = None,
    revision_graph_loader: RevisionGraphLoader | None = None,
) -> None:
    checker_kwargs: dict[str, object] = (
        {"code_heads_loader": code_heads_loader} if code_heads_loader is not None else {}
    )
    bootstrap_kwargs: dict[str, object] = dict(checker_kwargs)
    if revision_graph_loader is not None:
        bootstrap_kwargs["revision_graph_loader"] = revision_graph_loader

    # -- auto --------------------------------------------------------------
    await reset_dedicated_database(database_url)
    await construct(database_url)

    settings_auto = migration_bootstrap_settings(database_url, database_migration_mode="auto")
    checker_auto = PostgresReadinessChecker(settings_auto, **checker_kwargs)  # type: ignore[arg-type]
    runner_auto = CountingMigrationRunner()
    bootstrap_auto = MigrationBootstrap(
        settings_auto,
        readiness_checker=checker_auto,
        runner=runner_auto,
        **bootstrap_kwargs,  # type: ignore[arg-type]
    )
    try:
        if expect_fail_closed:
            with pytest.raises(MigrationSchemaInvalidError):
                await bootstrap_auto.ensure_schema_current()
            assert runner_auto.invocations == 0
        else:
            await bootstrap_auto.ensure_schema_current()
            assert runner_auto.invocations == (1 if expect_migrate else 0)
            assert (await checker_auto.check()).ready is True
    finally:
        await checker_auto.dispose()

    # -- verify_only ---------------------------------------------------------
    await reset_dedicated_database(database_url)
    await construct(database_url)

    settings_vo = migration_bootstrap_settings(database_url, database_migration_mode="verify_only")
    checker_vo = PostgresReadinessChecker(settings_vo, **checker_kwargs)  # type: ignore[arg-type]
    runner_vo = CountingMigrationRunner()
    bootstrap_vo = MigrationBootstrap(settings_vo, readiness_checker=checker_vo, runner=runner_vo)
    try:
        if expect_migrate or expect_fail_closed:
            # verify_only never distinguishes "would auto-migrate" from
            # "fails closed under auto" -- anything short of exact head is
            # simply not_ready, and Alembic is never invoked either way.
            with pytest.raises(MigrationClassificationFailedError):
                await bootstrap_vo.ensure_schema_current()
        else:
            await bootstrap_vo.ensure_schema_current()
        assert runner_vo.invocations == 0
    finally:
        await checker_vo.dispose()


# ---------------------------------------------------------------------------
# The nine states.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_pristine_fresh_schema(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(
        dedicated_database_url, construct=_construct_nothing, expect_migrate=True
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_unversioned_non_empty_schema(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(
        dedicated_database_url,
        construct=_construct_unversioned_non_empty,
        expect_fail_closed=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_exact_head(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(dedicated_database_url, construct=_construct_exact_head)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_known_ancestor(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(
        dedicated_database_url, construct=_construct_known_ancestor, expect_migrate=True
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_code_multiple_heads(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
    branch_revision_graph: tuple[RevisionGraph, frozenset[str]],
) -> None:
    graph, heads = branch_revision_graph
    assert len(heads) == 2
    await _run_matrix_state(
        dedicated_database_url,
        construct=_construct_nothing,
        expect_fail_closed=True,
        code_heads_loader=lambda: heads,
        revision_graph_loader=lambda: graph,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_version_table_empty(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(
        dedicated_database_url,
        construct=_construct_version_table_empty,
        expect_fail_closed=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_database_multiple_revisions(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(
        dedicated_database_url,
        construct=_construct_database_multiple_revisions,
        expect_fail_closed=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_known_non_ancestor(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
    branch_revision_graph: tuple[RevisionGraph, frozenset[str]],
) -> None:
    graph, heads = branch_revision_graph
    assert "mtx_branch_a" in heads
    single_head = frozenset({"mtx_branch_a"})
    await _run_matrix_state(
        dedicated_database_url,
        construct=_construct_known_non_ancestor,
        expect_fail_closed=True,
        code_heads_loader=lambda: single_head,
        revision_graph_loader=lambda: graph,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_matrix_unrecognized_revision(
    dedicated_database_url: str,  # noqa: F811 - reused fixture, imported above
) -> None:
    await _run_matrix_state(
        dedicated_database_url,
        construct=_construct_unrecognized_revision,
        expect_fail_closed=True,
    )
