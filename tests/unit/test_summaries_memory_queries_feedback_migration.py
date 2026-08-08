from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0006 = MIGRATIONS_VERSIONS / "0006_create_summaries_memory_queries_feedback.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.executed_statements: list[str] = []
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []

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
    summary_target_type: ENUM
    memory_entry_type: ENUM
    SUMMARIES_ANN_INDEX_SQL: str
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location("test_sm210_migration", MIGRATION_0006)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load SM-210 migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0006.read_text(encoding="utf-8")


def upgrade_result() -> tuple[list[str], OperationSpy]:
    module = load_migration_module()
    operation_spy = OperationSpy()
    created_enums: list[str] = []

    def create_summary_target_type(bind: object) -> None:
        if bind == "bind":
            created_enums.append("summary_target_type")

    def create_memory_entry_type(bind: object) -> None:
        if bind == "bind":
            created_enums.append("memory_entry_type")

    module.summary_target_type.create = create_summary_target_type
    module.memory_entry_type.create = create_memory_entry_type
    module.op = operation_spy

    module.upgrade()

    return created_enums, operation_spy


def table_columns(objects: tuple[object, ...]) -> dict[str, Column[object]]:
    return {item.name: item for item in objects if isinstance(item, Column)}


def check_sql(objects: tuple[object, ...]) -> dict[str | None, str]:
    return {item.name: str(item.sqltext) for item in objects if isinstance(item, CheckConstraint)}


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def test_sm210_revision_is_current_head() -> None:
    revision_files = sorted(path.name for path in MIGRATIONS_VERSIONS.glob("*.py"))

    assert revision_files == [
        "0001_enable_required_extensions.py",
        "0002_create_datasets.py",
        "0003_create_sources_and_documents.py",
        "0004_create_chunks.py",
        "0005_create_entities_relations.py",
        "0006_create_summaries_memory_queries_feedback.py",
    ]


def test_sm210_revision_metadata_and_enums() -> None:
    module = load_migration_module()

    assert module.revision == "0006"
    assert module.down_revision == "0005"
    assert module.branch_labels is None
    assert module.depends_on is None
    assert module.summary_target_type.name == "summary_target_type"
    assert module.summary_target_type.enums == ["document", "entity", "dataset", "cluster"]
    assert module.memory_entry_type.name == "memory_entry_type"
    assert module.memory_entry_type.enums == ["text", "qa", "feedback", "note"]


def test_upgrade_creates_enums_and_exact_sm210_tables() -> None:
    created_enums, operation_spy = upgrade_result()

    assert created_enums == ["summary_target_type", "memory_entry_type"]
    assert set(operation_spy.created_tables) == {
        "feedback",
        "memory_entries",
        "queries",
        "summaries",
    }


def test_summaries_migration_contract_and_indexes() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["summaries"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)
    checks = check_sql(objects)

    assert list(columns) == [
        "id",
        "dataset_id",
        "generation",
        "target_type",
        "target_id",
        "level",
        "text",
        "embedding",
        "is_active",
        "created_at",
    ]
    assert isinstance(columns["target_type"].type, ENUM)
    assert columns["target_id"].nullable is True
    assert len(columns["target_id"].foreign_keys) == 0
    assert isinstance(columns["embedding"].type, Vector)
    assert columns["embedding"].type.dim == 3072
    assert columns["embedding"].nullable is False
    assert columns["created_at"].type.timezone is True
    assert fks["fk_summaries_dataset_id_datasets"].ondelete == "RESTRICT"
    assert checks["ck_summaries_generation_non_negative"] == "generation >= 0"
    assert operation_spy.created_indexes["ix_summaries_dataset_id_generation_is_active"] == (
        "summaries",
        ("dataset_id", "generation", "is_active"),
        {},
    )
    assert operation_spy.executed_statements == [
        "CREATE INDEX ix_summaries_embedding_halfvec_hnsw "
        "ON summaries USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)"
    ]


def test_memory_entries_migration_contract_and_indexes() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["memory_entries"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)

    assert list(columns) == [
        "id",
        "dataset_id",
        "source_id",
        "session_id",
        "entry_type",
        "content",
        "metadata",
        "created_at",
    ]
    assert columns["source_id"].nullable is True
    assert columns["session_id"].nullable is True
    assert isinstance(columns["entry_type"].type, ENUM)
    assert isinstance(columns["metadata"].type, JSONB)
    assert fks["fk_memory_entries_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_memory_entries_source_id_sources"].ondelete == "SET NULL"
    assert operation_spy.created_indexes["ix_memory_entries_dataset_id"] == (
        "memory_entries",
        ("dataset_id",),
        {},
    )
    assert operation_spy.created_indexes["ix_memory_entries_source_id"] == (
        "memory_entries",
        ("source_id",),
        {},
    )


def test_queries_migration_contract_supports_nullable_content_without_hashes() -> None:
    _, operation_spy = upgrade_result()
    columns = table_columns(operation_spy.created_tables["queries"])

    assert list(columns) == [
        "id",
        "query_text",
        "dataset_ids",
        "mode",
        "answer",
        "references",
        "timings",
        "model",
        "created_at",
    ]
    assert columns["query_text"].nullable is True
    assert columns["answer"].nullable is True
    assert columns["model"].nullable is True
    assert isinstance(columns["dataset_ids"].type, ARRAY)
    assert columns["dataset_ids"].type.compile(dialect=postgresql.dialect()) == "UUID[]"
    assert len(columns["dataset_ids"].foreign_keys) == 0
    assert isinstance(columns["references"].type, JSONB)
    assert isinstance(columns["timings"].type, JSONB)
    assert "query_hash" not in columns
    assert "answer_hash" not in columns


def test_feedback_migration_contract_score_fk_and_index() -> None:
    _, operation_spy = upgrade_result()
    objects = operation_spy.created_tables["feedback"]
    columns = table_columns(objects)
    fks = foreign_keys(objects)
    checks = check_sql(objects)

    assert list(columns) == [
        "id",
        "query_id",
        "target_type",
        "target_id",
        "score",
        "comment",
        "applied_at",
        "created_at",
    ]
    assert columns["target_id"].nullable is True
    assert len(columns["target_id"].foreign_keys) == 0
    assert columns["score"].type.python_type is int
    assert checks["ck_feedback_score_allowed_values"] == "score IN (-1, 0, 1)"
    assert columns["comment"].nullable is True
    assert columns["applied_at"].nullable is True
    assert columns["applied_at"].type.timezone is True
    assert fks["fk_feedback_query_id_queries"].ondelete == "RESTRICT"
    assert operation_spy.created_indexes["ix_feedback_query_id"] == (
        "feedback",
        ("query_id",),
        {},
    )


def test_summary_ann_sql_preserves_adr_0006_strategy() -> None:
    module = load_migration_module()
    ann_sql = module.SUMMARIES_ANN_INDEX_SQL

    assert "USING hnsw" in ann_sql
    assert "embedding::halfvec(3072)" in ann_sql
    assert "halfvec_cosine_ops" in ann_sql
    assert "vector_l2_ops" not in ann_sql
    assert "vector_ip_ops" not in ann_sql
    assert "ivfflat" not in ann_sql.lower()
    assert "1536" not in ann_sql


def test_downgrade_removes_only_sm210_objects() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    dropped_enums: list[str] = []

    def drop_summary_target_type(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("summary_target_type")

    def drop_memory_entry_type(bind: object) -> None:
        if bind == "bind":
            dropped_enums.append("memory_entry_type")

    module.summary_target_type.drop = drop_summary_target_type
    module.memory_entry_type.drop = drop_memory_entry_type
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_indexes == [
        ("ix_feedback_query_id", "feedback"),
        ("ix_memory_entries_source_id", "memory_entries"),
        ("ix_memory_entries_dataset_id", "memory_entries"),
        ("ix_summaries_dataset_id_generation_is_active", "summaries"),
    ]
    assert operation_spy.executed_statements == ["DROP INDEX ix_summaries_embedding_halfvec_hnsw"]
    assert operation_spy.dropped_tables == ["feedback", "queries", "memory_entries", "summaries"]
    assert dropped_enums == ["memory_entry_type", "summary_target_type"]


def test_migration_does_not_create_sm211_or_forbidden_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "CASCADE" not in text
    assert "PIPELINE_RUNS" not in text
    assert "PIPELINE_STEPS" not in text
    assert "GRAPH_OUTBOX" not in text
    assert "QUERY_HASH" not in text
    assert "ANSWER_HASH" not in text
    assert "VECTOR(1536)" not in text
    assert "IVFFLAT" not in text
    assert "VECTOR_L2_OPS" not in text
    assert "VECTOR_IP_OPS" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "DELETED_AT" not in text
