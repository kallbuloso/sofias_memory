from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from sqlalchemy import CheckConstraint, Column, PrimaryKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import CITEXT, ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0001 = MIGRATIONS_VERSIONS / "0001_enable_required_extensions.py"
MIGRATION_0002 = MIGRATIONS_VERSIONS / "0002_create_datasets.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    dataset_status: ENUM
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_dataset_migration", MIGRATION_0002)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load dataset migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0002.read_text(encoding="utf-8")


def upgrade_objects() -> tuple[bool, tuple[object, ...]]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dataset_status_created = False

    def create_dataset_status(bind: object) -> None:
        nonlocal dataset_status_created
        dataset_status_created = bind == "bind"

    module.dataset_status.create = create_dataset_status
    module.op = operation_spy

    module.upgrade()

    return dataset_status_created, operation_spy.created_tables["datasets"]


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def unique_constraint_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, UniqueConstraint)}


def check_constraint_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, CheckConstraint)}


def check_constraint_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def test_dataset_revision_is_current_head() -> None:
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
    ]


def test_dataset_revision_metadata() -> None:
    module = load_migration_module()

    assert module.revision == "0002"
    assert module.down_revision == "0001"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_dataset_status_before_datasets() -> None:
    dataset_status_created, objects = upgrade_objects()

    assert dataset_status_created is True
    assert table_columns(objects)


def test_upgrade_creates_expected_dataset_columns() -> None:
    _, objects = upgrade_objects()
    columns = table_columns(objects)

    assert list(columns) == [
        "id",
        "name",
        "slug",
        "description",
        "status",
        "active_generation",
        "created_at",
        "updated_at",
    ]


def test_migration_column_types_and_nullability() -> None:
    _, objects = upgrade_objects()
    columns = table_columns(objects)

    assert isinstance(columns["id"].type, PostgreSQLUUID)
    assert isinstance(columns["name"].type, CITEXT)
    assert columns["slug"].type.python_type is str
    assert columns["description"].nullable is True
    assert isinstance(columns["status"].type, ENUM)
    assert columns["status"].type.name == "dataset_status"
    assert columns["status"].type.enums == ["active", "deleting", "deleted"]
    assert columns["status"].nullable is False
    assert columns["active_generation"].nullable is False
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True


def test_migration_dataset_status_enum_definition_is_exact() -> None:
    module = load_migration_module()

    assert isinstance(module.dataset_status, ENUM)
    assert module.dataset_status.name == "dataset_status"
    assert module.dataset_status.enums == ["active", "deleting", "deleted"]
    assert module.dataset_status.create_type is False


def test_migration_constraints_match_model_contract() -> None:
    _, objects = upgrade_objects()

    assert any(isinstance(item, PrimaryKeyConstraint) for item in objects)
    assert {"uq_datasets_name", "uq_datasets_slug"} <= unique_constraint_names(objects)
    assert {
        "ck_datasets_name_not_blank",
        "ck_datasets_name_max_length",
        "ck_datasets_slug_not_blank",
    } <= check_constraint_names(objects)


def test_migration_name_length_constraint_matches_model_contract() -> None:
    _, objects = upgrade_objects()
    columns = table_columns(objects)
    checks = check_constraint_sql(objects)

    assert isinstance(columns["name"].type, CITEXT)
    assert checks["ck_datasets_name_max_length"] == "char_length(name::text) <= 120"


def test_downgrade_removes_table_then_dataset_status() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dataset_status_dropped = False

    def drop_dataset_status(bind: object) -> None:
        nonlocal dataset_status_dropped
        dataset_status_dropped = bind == "bind"

    module.dataset_status.drop = drop_dataset_status
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_tables == ["datasets"]
    assert dataset_status_dropped is True


def test_migration_does_not_change_initial_extension_migration() -> None:
    assert "CREATE EXTENSION IF NOT EXISTS vector" in MIGRATION_0001.read_text(encoding="utf-8")


def test_migration_does_not_create_extensions_or_future_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "HNSW" not in text
    assert "VECTOR(3072)" not in text
    assert "CREATE INDEX" not in text
    assert "SOURCE" not in text
    assert "DOCUMENT" not in text
    assert "CHUNK" not in text


def test_migration_has_no_forbidden_ownership_or_soft_delete_columns() -> None:
    text = migration_text().lower()

    assert "tenant_id" not in text
    assert "owner_id" not in text
    assert "user_id" not in text
    assert "deleted_at" not in text
    assert "metadata" not in text


def test_downgrade_does_not_use_cascade_or_remove_extensions() -> None:
    text = migration_text().upper()

    assert "CASCADE" not in text
    assert "DROP EXTENSION" not in text
