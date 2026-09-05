from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ENUM, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0002 = MIGRATIONS_VERSIONS / "0002_create_datasets.py"
MIGRATION_0003 = MIGRATIONS_VERSIONS / "0003_create_sources_and_documents.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def get_bind(self) -> str:
        return "bind"

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def create_index(
        self, name: str, table_name: str, columns: list[str], **kwargs: object
    ) -> None:
        self.created_indexes[name] = (table_name, tuple(columns), kwargs)

    def drop_index(self, name: str, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    source_kind: ENUM
    source_status: ENUM
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location(
        "test_sources_documents_migration", MIGRATION_0003
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load sources/documents migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0003.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def create_source_kind(bind: object) -> None:
        if bind == "bind":
            created_enums.append("source_kind")

    def create_source_status(bind: object) -> None:
        if bind == "bind":
            created_enums.append("source_status")

    module.source_kind.create = create_source_kind
    module.source_status.create = create_source_status
    module.op = operation_spy

    module.upgrade()

    return created_enums, operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def unique_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, UniqueConstraint)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def test_sources_documents_revision_is_current_head() -> None:
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


def test_sources_documents_revision_metadata() -> None:
    module = load_migration_module()

    assert module.revision == "0003"
    assert module.down_revision == "0002"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_migration_enum_definitions_are_exact() -> None:
    module = load_migration_module()

    assert module.source_kind.name == "source_kind"
    assert module.source_kind.enums == ["text", "file", "url"]
    assert module.source_kind.create_type is False
    assert module.source_status.name == "source_status"
    assert module.source_status.enums == [
        "pending",
        "processing",
        "active",
        "failed",
        "deleting",
        "deleted",
    ]
    assert module.source_status.create_type is False


def test_upgrade_creates_enums_sources_and_documents() -> None:
    created_enums, operation_spy = upgrade_result()

    assert created_enums == ["source_kind", "source_status"]
    assert set(operation_spy.created_tables) == {"documents", "sources"}


def test_sources_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["sources"]
    columns = table_columns(objects)

    assert list(columns) == [
        "id",
        "dataset_id",
        "kind",
        "name",
        "mime_type",
        "original_uri",
        "storage_uri",
        "content_sha256",
        "normalized_sha256",
        "byte_size",
        "metadata",
        "status",
        "version",
        "created_at",
        "updated_at",
    ]
    assert isinstance(columns["metadata"].type, JSONB)
    assert columns["content_sha256"].type.length == 64
    assert columns["normalized_sha256"].nullable is True
    assert columns["created_at"].type.timezone is True
    assert columns["updated_at"].type.timezone is True


def test_sources_constraints_indexes_and_fk_policy() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["sources"]
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert "uq_sources_dataset_id_content_sha256_version" in unique_names(objects)
    assert checks["ck_sources_content_sha256_hex"] == "content_sha256 ~ '^[0-9a-fA-F]{64}$'"
    assert (
        checks["ck_sources_normalized_sha256_hex"]
        == "normalized_sha256 IS NULL OR normalized_sha256 ~ '^[0-9a-fA-F]{64}$'"
    )
    assert fks["fk_sources_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_sources_dataset_id_datasets"].ondelete == "RESTRICT"
    assert operation_spy.created_indexes["ix_sources_metadata"] == (
        "sources",
        ("metadata",),
        {"postgresql_using": "gin"},
    )
    assert operation_spy.created_indexes["ix_sources_status"] == ("sources", ("status",), {})


def test_documents_table_contract() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["documents"]
    columns = table_columns(objects)

    assert list(columns) == [
        "id",
        "dataset_id",
        "source_id",
        "generation",
        "title",
        "language",
        "normalized_text",
        "text_sha256",
        "token_count",
        "metadata",
        "is_active",
        "created_at",
    ]
    assert columns["language"].type.length == 16
    assert columns["text_sha256"].type.length == 64
    assert isinstance(columns["metadata"].type, JSONB)
    assert columns["created_at"].type.timezone is True
    assert "updated_at" not in columns


def test_documents_constraints_indexes_and_fk_policy() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["documents"]
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert checks["ck_documents_text_sha256_hex"] == "text_sha256 ~ '^[0-9a-fA-F]{64}$'"
    assert fks["fk_documents_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_documents_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_documents_source_id_sources"].elements[0].target_fullname == "sources.id"
    assert fks["fk_documents_source_id_sources"].ondelete == "RESTRICT"
    assert operation_spy.created_indexes["ix_documents_dataset_id_generation"] == (
        "documents",
        ("dataset_id", "generation"),
        {},
    )
    assert operation_spy.created_indexes["ix_documents_source_id_generation"] == (
        "documents",
        ("source_id", "generation"),
        {},
    )
    active_table, active_columns, active_kwargs = operation_spy.created_indexes[
        "ix_documents_active_generation"
    ]
    assert active_table == "documents"
    assert active_columns == ("dataset_id", "generation")
    assert str(active_kwargs["postgresql_where"]) == "is_active IS TRUE"


def test_downgrade_removes_only_sm207_objects() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def drop_source_kind(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("source_kind")

    def drop_source_status(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("source_status")

    module.source_kind.drop = drop_source_kind
    module.source_status.drop = drop_source_status
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_tables == ["documents", "sources"]
    assert dropped_enums == ["source_status", "source_kind"]


def test_migration_does_not_change_datasets_migration() -> None:
    text = MIGRATION_0002.read_text(encoding="utf-8")

    assert '"datasets"' in text
    assert 'revision: str = "0002"' in text


def test_migration_does_not_create_disallowed_or_future_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "CASCADE" not in text
    assert "CHUNKS" not in text
    assert "VECTOR" not in text
    assert "HNSW" not in text
    assert "TSVECTOR" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "DELETED_AT" not in text
