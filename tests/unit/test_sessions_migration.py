from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0012 = MIGRATIONS_VERSIONS / "0012_create_sessions_foundation.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.added_columns: dict[str, list[Column[object]]] = {}
        self.created_foreign_keys: dict[
            str, tuple[str, str, tuple[str, ...], tuple[str, ...], dict[str, object]]
        ] = {}
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []
        self.dropped_constraints: list[tuple[str, str, str | None]] = []
        self.dropped_columns: list[tuple[str, str]] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def create_index(
        self,
        name: str,
        table_name: str,
        columns: list[str],
        **kwargs: object,
    ) -> None:
        self.created_indexes[name] = (table_name, tuple(columns), kwargs)

    def add_column(self, table_name: str, column: Column[object]) -> None:
        self.added_columns.setdefault(table_name, []).append(column)

    def create_foreign_key(
        self,
        name: str,
        source_table: str,
        referent_table: str,
        local_cols: list[str],
        remote_cols: list[str],
        **kwargs: object,
    ) -> None:
        self.created_foreign_keys[name] = (
            source_table,
            referent_table,
            tuple(local_cols),
            tuple(remote_cols),
            kwargs,
        )

    def drop_index(self, name: str, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)

    def drop_constraint(self, name: str, table_name: str, type_: str | None = None) -> None:
        self.dropped_constraints.append((name, table_name, type_))

    def drop_column(self, table_name: str, column_name: str) -> None:
        self.dropped_columns.append((table_name, column_name))


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    session_status: ENUM
    SESSION_ID_MAX_LENGTH: int
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm601_migration", MIGRATION_0012)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-601 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0012.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def create_session_status(bind: object) -> None:
        if bind == "bind":
            created_enums.append("session_status")

    module.session_status.create = create_session_status
    module.op = operation_spy

    module.upgrade()

    return created_enums, operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def test_sm601_revision_is_current_head() -> None:
    revision_files = sorted(path.name for path in MIGRATIONS_VERSIONS.glob("*.py"))

    assert revision_files == [
        "0001_enable_required_extensions.py",
        "0002_create_datasets.py",
        "0003_create_sources_and_documents.py",
        "0004_create_chunks.py",
        "0005_create_entities_relations.py",
        "0006_create_summaries_memory_queries_feedback.py",
        "0007_create_pipeline_and_graph_outbox.py",
        "0008_pipeline_run_retry_and_operational_constraints.py",
        "0009_graph_outbox_processing_lease.py",
        "0010_pipeline_runs_operational_unique_constraint.py",
        "0011_add_dataset_delete_pipeline_type.py",
        "0012_create_sessions_foundation.py",
        "0013_session_entry_external_id_trim_invariant.py",
    ]


def test_sm601_revision_metadata_and_enum_definition_are_exact() -> None:
    module = load_migration_module()

    assert module.revision == "0012"
    assert module.down_revision == "0011"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert module.session_status.name == "session_status"
    assert module.session_status.enums == ["active", "archived"]
    assert module.SESSION_ID_MAX_LENGTH == 255


def test_upgrade_creates_enum_and_exact_sm601_tables() -> None:
    created_enums, operation_spy = upgrade_result()

    assert created_enums == ["session_status"]
    assert set(operation_spy.created_tables) == {"sessions", "session_entries"}


def test_sessions_migration_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["sessions"]
    columns = table_columns(objects)
    checks = check_sql(objects)

    assert list(columns) == [
        "id",
        "key",
        "name",
        "status",
        "metadata",
        "created_at",
        "updated_at",
        "archived_at",
    ]
    assert columns["key"].nullable is False
    assert columns["name"].nullable is True
    assert isinstance(columns["status"].type, ENUM)
    assert columns["status"].server_default is not None
    assert isinstance(columns["metadata"].type, JSONB)
    assert columns["metadata"].nullable is False
    assert columns["metadata"].server_default is not None
    assert columns["archived_at"].nullable is True
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True
    assert checks["ck_sessions_key_not_blank"] == "length(btrim(key)) > 0"
    assert checks["ck_sessions_key_max_length"] == "char_length(key) <= 255"
    assert checks["ck_sessions_name_max_length"] == "name IS NULL OR char_length(name) <= 120"
    assert not foreign_keys(objects)


def test_session_entries_migration_contract_and_index() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["session_entries"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)
    checks = check_sql(objects)

    assert list(columns) == [
        "id",
        "session_id",
        "external_id",
        "role",
        "content",
        "metadata",
        "created_at",
    ]
    assert columns["session_id"].nullable is False
    assert columns["external_id"].nullable is True
    assert columns["role"].nullable is False
    assert columns["content"].nullable is False
    assert isinstance(columns["metadata"].type, JSONB)
    assert fks["fk_session_entries_session_id_sessions"].ondelete == "CASCADE"
    assert checks["ck_session_entries_external_id_not_blank"] == (
        "external_id IS NULL OR length(btrim(external_id)) > 0"
    )
    assert checks["ck_session_entries_external_id_max_length"] == (
        "external_id IS NULL OR char_length(external_id) <= 255"
    )
    assert operation_spy.created_indexes["ix_session_entries_session_id_created_at_id"] == (
        "session_entries",
        ("session_id", "created_at", "id"),
        {},
    )
    table_name, columns_tuple, kwargs = operation_spy.created_indexes[
        "uq_session_entries_session_id_external_id"
    ]
    assert table_name == "session_entries"
    assert columns_tuple == ("session_id", "external_id")
    assert kwargs["unique"] is True
    assert str(kwargs["postgresql_where"]) == "external_id IS NOT NULL"


def test_upgrade_alters_queries_with_session_columns_fk_and_index() -> None:
    _, operation_spy = upgrade_result()

    added = {column.name: column for column in operation_spy.added_columns["queries"]}
    assert set(added) == {"session_id", "session_context_entry_ids"}
    assert added["session_id"].nullable is True
    assert added["session_context_entry_ids"].nullable is False
    assert isinstance(added["session_context_entry_ids"].type, ARRAY)

    fk_table, fk_referent, fk_local, fk_remote, fk_kwargs = operation_spy.created_foreign_keys[
        "fk_queries_session_id_sessions"
    ]
    assert fk_table == "queries"
    assert fk_referent == "sessions"
    assert fk_local == ("session_id",)
    assert fk_remote == ("id",)
    assert fk_kwargs["ondelete"] == "SET NULL"

    assert operation_spy.created_indexes["ix_queries_session_id_created_at"] == (
        "queries",
        ("session_id", "created_at"),
        {},
    )


def test_upgrade_alters_pipeline_runs_with_session_column_fk_and_index() -> None:
    _, operation_spy = upgrade_result()

    added = {column.name: column for column in operation_spy.added_columns["pipeline_runs"]}
    assert set(added) == {"session_id"}
    assert added["session_id"].nullable is True

    fk_table, fk_referent, fk_local, fk_remote, fk_kwargs = operation_spy.created_foreign_keys[
        "fk_pipeline_runs_session_id_sessions"
    ]
    assert fk_table == "pipeline_runs"
    assert fk_referent == "sessions"
    assert fk_local == ("session_id",)
    assert fk_remote == ("id",)
    assert fk_kwargs["ondelete"] == "SET NULL"

    assert operation_spy.created_indexes["ix_pipeline_runs_session_id"] == (
        "pipeline_runs",
        ("session_id",),
        {},
    )


def test_downgrade_removes_only_sm601_objects_in_reverse_order() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def drop_session_status(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("session_status")

    module.session_status.drop = drop_session_status
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_indexes == [
        ("ix_pipeline_runs_session_id", "pipeline_runs"),
        ("ix_queries_session_id_created_at", "queries"),
        ("uq_session_entries_session_id_external_id", "session_entries"),
        ("ix_session_entries_session_id_created_at_id", "session_entries"),
    ]
    assert operation_spy.dropped_constraints == [
        ("fk_pipeline_runs_session_id_sessions", "pipeline_runs", "foreignkey"),
        ("fk_queries_session_id_sessions", "queries", "foreignkey"),
    ]
    assert operation_spy.dropped_columns == [
        ("pipeline_runs", "session_id"),
        ("queries", "session_context_entry_ids"),
        ("queries", "session_id"),
    ]
    assert operation_spy.dropped_tables == ["session_entries", "sessions"]
    assert dropped_enums == ["session_status"]


def test_migration_does_not_backfill_or_touch_forbidden_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "DROP EXTENSION" not in text
    assert "UPDATE " not in text
    assert "INSERT INTO" not in text
    assert "MEMORY_ENTRIES" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "DELETED_AT" not in text
    assert "LAST_ACTIVE_AT" not in text
    assert "EXPIRES_AT" not in text
    assert "CITEXT" not in text
    assert "NEO4J" not in text
    assert "GRAPH_OUTBOX" not in text
