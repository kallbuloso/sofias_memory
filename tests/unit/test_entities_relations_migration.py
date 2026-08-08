from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Protocol

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, Column, ForeignKeyConstraint, PrimaryKeyConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

MIGRATIONS_VERSIONS = Path(__file__).resolve().parents[2] / "migrations" / "versions"
MIGRATION_0005 = MIGRATIONS_VERSIONS / "0005_create_entities_relations.py"


class OperationSpy:
    def __init__(self) -> None:
        self.created_tables: dict[str, tuple[object, ...]] = {}
        self.created_indexes: dict[str, tuple[str, tuple[str, ...], dict[str, object]]] = {}
        self.dropped_indexes: list[tuple[str, str | None]] = []
        self.dropped_tables: list[str] = []

    def f(self, name: str) -> str:
        return name

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

    def drop_index(self, name: str, table_name: str | None = None) -> None:
        self.dropped_indexes.append((name, table_name))

    def drop_table(self, name: str) -> None:
        self.dropped_tables.append(name)


class MigrationModule(Protocol):
    revision: str
    down_revision: str
    branch_labels: str | None
    depends_on: str | None
    op: OperationSpy

    def upgrade(self) -> None: ...
    def downgrade(self) -> None: ...


def load_migration_module() -> MigrationModule:
    spec = importlib.util.spec_from_file_location(
        "test_entities_relations_migration", MIGRATION_0005
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load entities/relations migration")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module  # type: ignore[return-value]


def migration_text() -> str:
    return MIGRATION_0005.read_text(encoding="utf-8")


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


def foreign_keys(objects: tuple[object, ...]) -> dict[str | None, ForeignKeyConstraint]:
    return {item.name: item for item in objects if isinstance(item, ForeignKeyConstraint)}


def primary_key_names(objects: tuple[object, ...]) -> set[str | None]:
    return {item.name for item in objects if isinstance(item, PrimaryKeyConstraint)}


def test_entities_relations_revision_is_current_head() -> None:
    revision_files = sorted(path.name for path in MIGRATIONS_VERSIONS.glob("*.py"))

    assert revision_files == [
        "0001_enable_required_extensions.py",
        "0002_create_datasets.py",
        "0003_create_sources_and_documents.py",
        "0004_create_chunks.py",
        "0005_create_entities_relations.py",
        "0006_create_summaries_memory_queries_feedback.py",
    ]


def test_entities_relations_revision_metadata() -> None:
    module = load_migration_module()

    assert module.revision == "0005"
    assert module.down_revision == "0004"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_entities_table_contract() -> None:
    columns = table_columns(upgrade_result().created_tables["entities"])

    assert list(columns) == [
        "id",
        "dataset_id",
        "generation",
        "canonical_key",
        "name",
        "entity_type",
        "description",
        "aliases",
        "properties",
        "confidence",
        "importance_weight",
        "embedding",
        "is_active",
        "created_at",
        "updated_at",
    ]
    assert isinstance(columns["aliases"].type, ARRAY)
    assert isinstance(columns["properties"].type, JSONB)
    assert isinstance(columns["embedding"].type, Vector)
    assert columns["embedding"].type.dim == 3072
    assert columns["embedding"].nullable is True


def test_entities_constraints_fks_and_partial_unique_index() -> None:
    operation_spy = upgrade_result()
    objects = operation_spy.created_tables["entities"]
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert checks["ck_entities_generation_non_negative"] == "generation >= 0"
    assert checks["ck_entities_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert fks["fk_entities_dataset_id_datasets"].ondelete == "RESTRICT"
    assert operation_spy.created_indexes["uq_entities_dataset_id_canonical_key_active"] == (
        "entities",
        ("dataset_id", "canonical_key"),
        {
            "unique": True,
            "postgresql_where": operation_spy.created_indexes[
                "uq_entities_dataset_id_canonical_key_active"
            ][2]["postgresql_where"],
        },
    )
    where = operation_spy.created_indexes["uq_entities_dataset_id_canonical_key_active"][2][
        "postgresql_where"
    ]
    assert str(where) == "is_active IS TRUE"


def test_entity_mentions_table_constraints_fks_and_indexes() -> None:
    operation_spy = upgrade_result()
    objects = operation_spy.created_tables["entity_mentions"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert list(columns) == [
        "id",
        "entity_id",
        "chunk_id",
        "surface_text",
        "start_char",
        "end_char",
        "confidence",
    ]
    assert "pk_entity_mentions" in primary_key_names(objects)
    assert 'sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_mentions"))' in migration_text()
    assert columns["start_char"].nullable is True
    assert columns["end_char"].nullable is True
    assert fks["fk_entity_mentions_entity_id_entities"].ondelete == "CASCADE"
    assert fks["fk_entity_mentions_chunk_id_chunks"].ondelete == "CASCADE"
    assert checks["ck_entity_mentions_start_char_non_negative"] == (
        "start_char IS NULL OR start_char >= 0"
    )
    assert checks["ck_entity_mentions_end_char_non_negative"] == (
        "end_char IS NULL OR end_char >= 0"
    )
    assert checks["ck_entity_mentions_char_offsets_valid"] == (
        "start_char IS NULL OR end_char IS NULL OR end_char >= start_char"
    )
    assert checks["ck_entity_mentions_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert operation_spy.created_indexes["ix_entity_mentions_entity_id"] == (
        "entity_mentions",
        ("entity_id",),
        {},
    )
    assert operation_spy.created_indexes["ix_entity_mentions_chunk_id"] == (
        "entity_mentions",
        ("chunk_id",),
        {},
    )


def test_relations_table_constraints_fks_and_indexes() -> None:
    operation_spy = upgrade_result()
    objects = operation_spy.created_tables["relations"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert list(columns) == [
        "id",
        "dataset_id",
        "generation",
        "source_entity_id",
        "target_entity_id",
        "predicate",
        "description",
        "properties",
        "confidence",
        "importance_weight",
        "embedding",
        "is_active",
        "created_at",
        "updated_at",
    ]
    assert isinstance(columns["properties"].type, JSONB)
    assert isinstance(columns["embedding"].type, Vector)
    assert columns["embedding"].type.dim == 3072
    assert columns["embedding"].nullable is True
    assert checks["ck_relations_generation_non_negative"] == "generation >= 0"
    assert checks["ck_relations_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert fks["fk_relations_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_relations_source_entity_id_entities"].ondelete == "RESTRICT"
    assert fks["fk_relations_target_entity_id_entities"].ondelete == "RESTRICT"
    assert operation_spy.created_indexes["ix_relations_dataset_id_is_active"] == (
        "relations",
        ("dataset_id", "is_active"),
        {},
    )
    assert operation_spy.created_indexes["ix_relations_source_entity_id"] == (
        "relations",
        ("source_entity_id",),
        {},
    )
    assert operation_spy.created_indexes["ix_relations_target_entity_id"] == (
        "relations",
        ("target_entity_id",),
        {},
    )
    assert "ix_relations_predicate" not in operation_spy.created_indexes


def test_relation_evidence_table_composite_pk_fks_and_index() -> None:
    operation_spy = upgrade_result()
    objects = operation_spy.created_tables["relation_evidence"]
    columns = table_columns(objects)
    checks = check_sql(objects)
    fks = foreign_keys(objects)

    assert list(columns) == ["relation_id", "chunk_id", "quote", "confidence"]
    assert "pk_relation_evidence" in primary_key_names(objects)
    assert (
        'sa.PrimaryKeyConstraint("relation_id", "chunk_id", name=op.f("pk_relation_evidence"))'
        in migration_text()
    )
    assert fks["fk_relation_evidence_relation_id_relations"].ondelete == "CASCADE"
    assert fks["fk_relation_evidence_chunk_id_chunks"].ondelete == "RESTRICT"
    assert checks["ck_relation_evidence_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert operation_spy.created_indexes["ix_relation_evidence_chunk_id"] == (
        "relation_evidence",
        ("chunk_id",),
        {},
    )
    assert "ix_relation_evidence_relation_id" not in operation_spy.created_indexes


def test_downgrade_removes_only_entities_relations_objects() -> None:
    module = load_migration_module()
    operation_spy = OperationSpy()
    module.op = operation_spy

    module.downgrade()

    assert operation_spy.dropped_indexes == [
        ("ix_relation_evidence_chunk_id", "relation_evidence"),
        ("ix_relations_target_entity_id", "relations"),
        ("ix_relations_source_entity_id", "relations"),
        ("ix_relations_dataset_id_is_active", "relations"),
        ("ix_entity_mentions_chunk_id", "entity_mentions"),
        ("ix_entity_mentions_entity_id", "entity_mentions"),
        ("uq_entities_dataset_id_canonical_key_active", "entities"),
    ]
    assert operation_spy.dropped_tables == [
        "relation_evidence",
        "relations",
        "entity_mentions",
        "entities",
    ]


def test_migration_does_not_create_sm_210_or_ann_schema() -> None:
    text = migration_text().upper()

    assert "CREATE EXTENSION" not in text
    assert "SUMMARIES" not in text
    assert "MEMORY_ENTRIES" not in text
    assert "QUERIES" not in text
    assert "FEEDBACK" not in text
    assert "HNSW" not in text
    assert "IVFFLAT" not in text
    assert "IX_RELATIONS_PREDICATE" not in text
    assert "IX_RELATION_EVIDENCE_RELATION_ID" not in text
    assert "VECTOR(1536)" not in text
    assert "OWNER_ID" not in text
    assert "TENANT_ID" not in text
    assert "USER_ID" not in text
    assert "DELETED_AT" not in text
    assert "DROP EXTENSION" not in text
    assert "DROP TYPE" not in text
    assert "DROP TABLE" not in text
