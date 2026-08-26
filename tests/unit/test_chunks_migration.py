from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0003 = MIGRATIONS_VERSIONS / "0003_create_sources_and_documents.py"
MIGRATION_0004 = MIGRATIONS_VERSIONS / "0004_create_chunks.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.executed_statements: list[str] = []
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

    def create_table(self, name: str, *objects: object) -> None:
        self.created_tables[name] = objects

    def create_index(
        self, name: str, table_name: str, columns: list[str], **kwargs: object
    ) -> None:
        self.created_indexes[name] = (table_name, tuple(columns), kwargs)

    def execute(self, statement: str) -> None:
        self.executed_statements.append(statement)

    def drop_index(self, name: str, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    CHUNKS_ANN_INDEX_SQL: str
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_chunks_migration", MIGRATION_0004)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load chunks migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0004.read_text(encoding="utf-8")


def upgrade_result() -> OperationSpy:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy

    module.upgrade()

    return operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def unique_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, UniqueConstraint)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def test_chunks_revision_is_current_head() -> None:
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
    ]


def test_chunks_revision_metadata() -> None:
    module = load_migration_module()

    assert module.revision == "0004"
    assert module.down_revision == "0003"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_chunks_table_contract() -> None:
    operation_spy = upgrade_result()
    columns = table_columns(operation_spy.created_tables["chunks"])

    assert list(columns) == [
        "id",
        "dataset_id",
        "document_id",
        "source_id",
        "generation",
        "ordinal",
        "text",
        "content_sha256",
        "token_count",
        "start_char",
        "end_char",
        "section_path",
        "metadata",
        "embedding",
        "lexical",
        "is_active",
        "created_at",
    ]


def test_chunks_column_types() -> None:
    operation_spy = upgrade_result()
    columns = table_columns(operation_spy.created_tables["chunks"])

    assert columns["content_sha256"].type.length == 64
    assert isinstance(columns["section_path"].type, ARRAY)
    assert columns["section_path"].type.item_type.python_type is str
    assert isinstance(columns["metadata"].type, JSONB)
    assert isinstance(columns["embedding"].type, Vector)
    assert columns["embedding"].type.dim == 3072
    assert isinstance(columns["lexical"].type, TSVECTOR)
    assert columns["created_at"].type.timezone is True
    assert "updated_at" not in columns


def test_chunks_constraints_unique_and_fk_policy() -> None:
    operation_spy = upgrade_result()
    objects = operation_spy.created_tables["chunks"]
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert checks["ck_chunks_content_sha256_hex"] == "content_sha256 ~ '^[0-9a-fA-F]{64}$'"
    assert checks["ck_chunks_generation_non_negative"] == "generation >= 0"
    assert checks["ck_chunks_ordinal_non_negative"] == "ordinal >= 0"
    assert checks["ck_chunks_token_count_non_negative"] == "token_count >= 0"
    assert checks["ck_chunks_char_offsets_valid"] == "start_char >= 0 AND end_char >= start_char"
    assert "uq_chunks_document_id_generation_ordinal" in unique_names(objects)
    assert fks["fk_chunks_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_chunks_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_chunks_document_id_documents"].elements[0].target_fullname == "documents.id"
    assert fks["fk_chunks_document_id_documents"].ondelete == "RESTRICT"
    assert fks["fk_chunks_source_id_sources"].elements[0].target_fullname == "sources.id"
    assert fks["fk_chunks_source_id_sources"].ondelete == "RESTRICT"


def test_chunks_indexes_and_ann_sql_are_exact() -> None:
    operation_spy = upgrade_result()

    assert operation_spy.created_indexes["ix_chunks_lexical"] == (
        "chunks",
        ("lexical",),
        {"postgresql_using": "gin"},
    )
    assert operation_spy.created_indexes["ix_chunks_dataset_id_is_active"] == (
        "chunks",
        ("dataset_id", "is_active"),
        {},
    )
    assert operation_spy.created_indexes["ix_chunks_source_id_is_active"] == (
        "chunks",
        ("source_id", "is_active"),
        {},
    )
    assert operation_spy.executed_statements == [
        "CREATE INDEX ix_chunks_embedding_halfvec_hnsw "
        "ON chunks USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)"
    ]


def test_ann_sql_preserves_adr_0006_strategy() -> None:
    module = load_migration_module()
    ann_sql = module.CHUNKS_ANN_INDEX_SQL

    assert "USING hnsw" in ann_sql
    assert "embedding::halfvec(3072)" in ann_sql
    assert "halfvec_cosine_ops" in ann_sql
    assert "vector_l2_ops" not in ann_sql
    assert "vector_ip_ops" not in ann_sql
    assert "vector_cosine_ops" not in ann_sql
    assert "ivfflat" not in ann_sql.lower()
    assert "1536" not in ann_sql


def test_downgrade_removes_only_chunks_objects() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.executed_statements == ["DROP INDEX ix_chunks_embedding_halfvec_hnsw"]
    assert operation_spy.dropped_indexes == [
        ("ix_chunks_source_id_is_active", "chunks"),
        ("ix_chunks_dataset_id_is_active", "chunks"),
        ("ix_chunks_lexical", "chunks"),
    ]
    assert operation_spy.dropped_tables == ["chunks"]


def test_migration_does_not_change_sources_documents_migration() -> None:
    text = MIGRATION_0003.read_text(encoding="utf-8")

    assert 'revision: str = "0003"' in text
    assert '"sources"' in text
    assert '"documents"' in text


def test_migration_does_not_create_disallowed_or_future_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "CASCADE" not in text
    assert "ENTITIES" not in text
    assert "RELATIONS" not in text
    assert "SUMMARIES" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "DELETED_AT" not in text
    assert "VECTOR(1536)" not in text
    assert "IVFFLAT" not in text
