from __future__ import annotations

import pytest

from sofias_memory.infrastructure.postgres.migration_state import (
    AlembicScriptDirectoryRevisionGraph,
    MigrationSchemaState,
    MigrationStateSnapshot,
    RevisionGraph,
    classify_migration_state,
    collect_migration_state_snapshot,
    load_revision_graph,
)

HEAD = "0017"


class FakeRevisionGraph:
    """Test double for :class:`RevisionGraph` -- a plain in-memory revision
    map, never a real Alembic ``ScriptDirectory``. Exists so the pure
    classifier logic in :func:`classify_migration_state` is testable without
    constructing a real (possibly branched) Alembic migration graph on disk."""

    def __init__(self, *, known: frozenset[str], ancestors: frozenset[str]) -> None:
        self._known = known
        self._ancestors = ancestors

    def is_known_revision(self, revision: str) -> bool:
        return revision in self._known

    def ancestors_of(self, head: str) -> frozenset[str]:
        assert head == HEAD
        return self._ancestors


def snapshot(**overrides: object) -> MigrationStateSnapshot:
    values: dict[str, object] = {
        "alembic_version_table_present": True,
        "database_revisions": frozenset({HEAD}),
        "application_base_tables_present": True,
        "code_heads": frozenset({HEAD}),
    }
    values.update(overrides)
    return MigrationStateSnapshot(**values)  # type: ignore[arg-type]


def graph(
    *, known: frozenset[str] = frozenset(), ancestors: frozenset[str] = frozenset()
) -> RevisionGraph:
    return FakeRevisionGraph(known=known, ancestors=ancestors)


# ---------------------------------------------------------------------------
# Nine-state matrix (Feature Contract v0.6.0 S4 / backlog SM-901 S20)
# ---------------------------------------------------------------------------


def test_alembic_version_absent_and_zero_base_tables_is_pristine_fresh_schema() -> None:
    state = classify_migration_state(
        snapshot(
            alembic_version_table_present=False,
            database_revisions=frozenset(),
            application_base_tables_present=False,
        ),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.PRISTINE_FRESH_SCHEMA


def test_alembic_version_absent_and_base_table_present_is_unversioned_non_empty_schema() -> None:
    state = classify_migration_state(
        snapshot(
            alembic_version_table_present=False,
            database_revisions=frozenset(),
            application_base_tables_present=True,
        ),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.UNVERSIONED_NON_EMPTY_SCHEMA


def test_alembic_version_present_with_zero_rows_is_version_table_empty() -> None:
    state = classify_migration_state(
        snapshot(
            alembic_version_table_present=True,
            database_revisions=frozenset(),
        ),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.VERSION_TABLE_EMPTY


def test_alembic_version_present_with_multiple_rows_is_database_multiple_revisions() -> None:
    state = classify_migration_state(
        snapshot(database_revisions=frozenset({"0016", HEAD})),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.DATABASE_MULTIPLE_REVISIONS


def test_single_revision_equal_to_head_is_exact_head() -> None:
    state = classify_migration_state(
        snapshot(database_revisions=frozenset({HEAD}), code_heads=frozenset({HEAD})),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.EXACT_HEAD


def test_single_revision_that_is_a_known_ancestor_is_known_ancestor() -> None:
    state = classify_migration_state(
        snapshot(database_revisions=frozenset({"0010"})),
        revision_graph=graph(known=frozenset({"0010"}), ancestors=frozenset({"0010", HEAD})),
    )

    assert state is MigrationSchemaState.KNOWN_ANCESTOR


def test_single_revision_resolvable_but_not_ancestor_is_known_non_ancestor() -> None:
    state = classify_migration_state(
        snapshot(database_revisions=frozenset({"branch-x"})),
        revision_graph=graph(known=frozenset({"branch-x"}), ancestors=frozenset({HEAD})),
    )

    assert state is MigrationSchemaState.KNOWN_NON_ANCESTOR


def test_single_revision_not_resolvable_is_unrecognized_revision() -> None:
    state = classify_migration_state(
        snapshot(database_revisions=frozenset({"0018"})),
        revision_graph=graph(known=frozenset(), ancestors=frozenset({HEAD})),
    )

    assert state is MigrationSchemaState.UNRECOGNIZED_REVISION


def test_multiple_code_heads_is_code_multiple_heads() -> None:
    state = classify_migration_state(
        snapshot(code_heads=frozenset({HEAD, "branch-head"})),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.CODE_MULTIPLE_HEADS


# ---------------------------------------------------------------------------
# Precedence (backlog SM-901 S21)
# ---------------------------------------------------------------------------


def test_multiple_code_heads_wins_over_a_revision_that_would_otherwise_be_known_ancestor() -> None:
    # Same raw inputs that would classify as KNOWN_ANCESTOR under a single
    # head must never leak through once code_heads itself is ambiguous.
    state = classify_migration_state(
        snapshot(
            code_heads=frozenset({HEAD, "branch-head"}),
            database_revisions=frozenset({"0010"}),
        ),
        revision_graph=graph(known=frozenset({"0010"}), ancestors=frozenset({"0010", HEAD})),
    )

    assert state is MigrationSchemaState.CODE_MULTIPLE_HEADS


def test_zero_code_heads_is_also_code_multiple_heads_never_a_database_state() -> None:
    # An application graph with no head at all is a broken-code scenario,
    # not a database-side classification -- it must fail closed the same
    # way an ambiguous multi-head graph does, never fall through to a
    # database-derived state.
    state = classify_migration_state(
        snapshot(code_heads=frozenset()),
        revision_graph=graph(),
    )

    assert state is MigrationSchemaState.CODE_MULTIPLE_HEADS


def test_zero_code_heads_never_falls_through_to_a_database_derived_state() -> None:
    # Zero code heads must resolve to CODE_MULTIPLE_HEADS regardless of what
    # the database side looks like -- including raw states that would,
    # under a single valid code head, classify as PRISTINE_FRESH_SCHEMA,
    # UNVERSIONED_NON_EMPTY_SCHEMA, VERSION_TABLE_EMPTY,
    # DATABASE_MULTIPLE_REVISIONS, EXACT_HEAD, or an ancestor/unrecognized
    # revision. The revision graph must never even be consulted once
    # code_heads itself is empty -- proven by exploding on any call.
    class ExplodingRevisionGraph:
        def is_known_revision(self, revision: str) -> bool:
            raise AssertionError("must not resolve a revision when code_heads is empty")

        def ancestors_of(self, head: str) -> frozenset[str]:
            raise AssertionError("must not walk ancestry when code_heads is empty")

    would_be_database_states = (
        # (alembic_version_table_present, database_revisions, application_base_tables_present)
        (False, frozenset(), False),  # would-be PRISTINE_FRESH_SCHEMA
        (False, frozenset(), True),  # would-be UNVERSIONED_NON_EMPTY_SCHEMA
        (True, frozenset(), False),  # would-be VERSION_TABLE_EMPTY
        (True, frozenset({"0010", HEAD}), True),  # would-be DATABASE_MULTIPLE_REVISIONS
        (True, frozenset({HEAD}), True),  # would-be EXACT_HEAD
        (True, frozenset({"0018"}), True),  # would-be ancestor/unrecognized revision
    )

    for (
        alembic_version_table_present,
        database_revisions,
        application_base_tables_present,
    ) in would_be_database_states:
        state = classify_migration_state(
            snapshot(
                code_heads=frozenset(),
                alembic_version_table_present=alembic_version_table_present,
                database_revisions=database_revisions,
                application_base_tables_present=application_base_tables_present,
            ),
            revision_graph=ExplodingRevisionGraph(),
        )
        assert state is MigrationSchemaState.CODE_MULTIPLE_HEADS


def test_alembic_version_absent_is_classified_before_any_revision_resolution_attempt() -> None:
    class ExplodingRevisionGraph:
        def is_known_revision(self, revision: str) -> bool:
            raise AssertionError(
                "must not attempt to resolve a revision when alembic_version is absent"
            )

        def ancestors_of(self, head: str) -> frozenset[str]:
            raise AssertionError("must not walk ancestry when alembic_version is absent")

    state = classify_migration_state(
        snapshot(
            alembic_version_table_present=False,
            database_revisions=frozenset(),
            application_base_tables_present=False,
        ),
        revision_graph=ExplodingRevisionGraph(),
    )

    assert state is MigrationSchemaState.PRISTINE_FRESH_SCHEMA


def test_unrecognized_revision_never_infers_future_or_older_from_the_string() -> None:
    # "9999" would sort lexically/numerically "after" 0017, and "0000" would
    # sort "before" it -- neither must influence the outcome, since ordering
    # comparison of revision strings is explicitly forbidden.
    for candidate in ("9999", "0000", "zzzz-unrelated"):
        state = classify_migration_state(
            snapshot(database_revisions=frozenset({candidate})),
            revision_graph=graph(known=frozenset(), ancestors=frozenset({HEAD})),
        )
        assert state is MigrationSchemaState.UNRECOGNIZED_REVISION


# ---------------------------------------------------------------------------
# Real Alembic graph proof (backlog SM-901 S22) -- no hardcoded "0017" inside
# production code, only in this test asserting today's actual repository state.
# ---------------------------------------------------------------------------


def test_real_script_directory_reports_the_actual_repository_head() -> None:
    revision_graph = load_revision_graph()

    assert isinstance(revision_graph, AlembicScriptDirectoryRevisionGraph)
    assert revision_graph.is_known_revision("0017")
    assert "0017" in revision_graph.ancestors_of("0017")


def test_real_script_directory_proves_a_genuine_known_ancestor() -> None:
    revision_graph = load_revision_graph()

    assert "0010" in revision_graph.ancestors_of("0017")
    assert revision_graph.is_known_revision("0010")


def test_real_script_directory_reports_a_future_revision_as_unrecognized() -> None:
    revision_graph = load_revision_graph()

    # "0018" does not exist in this repository's migrations/versions/ at this
    # baseline -- resolved via Alembic's own official API, never via string
    # comparison against the real head "0017".
    assert not revision_graph.is_known_revision("0018")
    assert "0018" not in revision_graph.ancestors_of("0017")


def test_real_classify_migration_state_end_to_end_for_unrecognized_revision() -> None:
    revision_graph = load_revision_graph()

    state = classify_migration_state(
        snapshot(database_revisions=frozenset({"0018"}), code_heads=frozenset({"0017"})),
        revision_graph=revision_graph,
    )

    assert state is MigrationSchemaState.UNRECOGNIZED_REVISION


def test_real_classify_migration_state_end_to_end_for_known_ancestor() -> None:
    revision_graph = load_revision_graph()

    state = classify_migration_state(
        snapshot(database_revisions=frozenset({"0010"}), code_heads=frozenset({"0017"})),
        revision_graph=revision_graph,
    )

    assert state is MigrationSchemaState.KNOWN_ANCESTOR


# ---------------------------------------------------------------------------
# Snapshot collection: read-only guarantee + table-absent vs. empty-table
# distinction (backlog SM-901 S6/S9/S24)
# ---------------------------------------------------------------------------


class FakeMappingResult:
    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = rows

    def __iter__(self) -> object:
        return iter(self._rows)


class FakeResult:
    def __init__(
        self, *, scalar: str | None = None, rows: tuple[dict[str, object], ...] = ()
    ) -> None:
        self._scalar = scalar
        self._rows = rows

    def scalar_one(self) -> str:
        if self._scalar is None:
            raise AssertionError("scalar result not configured")
        return self._scalar

    def first(self) -> dict[str, object] | None:
        return self._rows[0] if self._rows else None

    def mappings(self) -> FakeMappingResult:
        return FakeMappingResult(self._rows)


class FakeConnection:
    def __init__(
        self,
        *,
        alembic_version_present: bool,
        revision_rows: tuple[str, ...] = (),
        application_base_table_present: bool,
    ) -> None:
        self._alembic_version_present = alembic_version_present
        self._revision_rows = revision_rows
        self._application_base_table_present = application_base_table_present
        self.executed_sql: list[str] = []

    async def execute(self, statement: object, params: object | None = None) -> FakeResult:
        sql = str(statement)
        self.executed_sql.append(sql)
        if "SELECT current_schema()" in sql:
            return FakeResult(scalar="public")
        if "table_name = 'alembic_version'" in sql:
            rows = ({"table_name": "alembic_version"},) if self._alembic_version_present else ()
            return FakeResult(rows=rows)
        if "SELECT version_num FROM alembic_version" in sql:
            return FakeResult(rows=tuple({"version_num": rev} for rev in self._revision_rows))
        if "table_name <> 'alembic_version'" in sql:
            rows = (
                ({"table_name": "some_app_table"},) if self._application_base_table_present else ()
            )
            return FakeResult(rows=rows)
        raise AssertionError(f"unexpected SQL: {sql}")


@pytest.mark.asyncio
async def test_snapshot_distinguishes_alembic_version_absent_from_present_with_zero_rows() -> None:
    absent = await collect_migration_state_snapshot(
        FakeConnection(alembic_version_present=False, application_base_table_present=False),  # type: ignore[arg-type]
        code_heads=frozenset({HEAD}),
    )
    empty = await collect_migration_state_snapshot(
        FakeConnection(
            alembic_version_present=True, revision_rows=(), application_base_table_present=False
        ),  # type: ignore[arg-type]
        code_heads=frozenset({HEAD}),
    )

    assert absent.alembic_version_table_present is False
    assert absent.database_revisions == frozenset()
    assert empty.alembic_version_table_present is True
    assert empty.database_revisions == frozenset()
    assert absent != empty


@pytest.mark.asyncio
async def test_snapshot_reports_multiple_revision_rows() -> None:
    result = await collect_migration_state_snapshot(
        FakeConnection(  # type: ignore[arg-type]
            alembic_version_present=True,
            revision_rows=("0016", "0017"),
            application_base_table_present=True,
        ),
        code_heads=frozenset({HEAD}),
    )

    assert result.database_revisions == frozenset({"0016", "0017"})


@pytest.mark.asyncio
async def test_snapshot_reports_application_base_tables_present() -> None:
    present = await collect_migration_state_snapshot(
        FakeConnection(
            alembic_version_present=True, revision_rows=(HEAD,), application_base_table_present=True
        ),  # type: ignore[arg-type]
        code_heads=frozenset({HEAD}),
    )
    absent = await collect_migration_state_snapshot(
        FakeConnection(alembic_version_present=False, application_base_table_present=False),  # type: ignore[arg-type]
        code_heads=frozenset({HEAD}),
    )

    assert present.application_base_tables_present is True
    assert absent.application_base_tables_present is False


@pytest.mark.asyncio
async def test_snapshot_collection_issues_only_read_only_statements() -> None:
    connection = FakeConnection(
        alembic_version_present=True,
        revision_rows=(HEAD,),
        application_base_table_present=True,
    )

    await collect_migration_state_snapshot(connection, code_heads=frozenset({HEAD}))  # type: ignore[arg-type]

    forbidden = ("CREATE ", "ALTER ", "DROP ", "INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ")
    for sql in connection.executed_sql:
        upper = sql.upper()
        for keyword in forbidden:
            assert keyword not in upper, f"non-read-only statement issued: {sql!r}"
    assert len(connection.executed_sql) == 4
