from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, ForeignKeyConstraint, Index
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.sql.schema import Column

from sofias_memory.infrastructure.postgres import Entity, EntityMention, Relation, RelationEvidence
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS


def column_names(model: type[object]) -> list[str]:
    return [column.name for column in model.__table__.columns]  # type: ignore[attr-defined]


def foreign_keys(model: type[object]) -> dict[str, ForeignKeyConstraint]:
    return {
        constraint.name: constraint
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, ForeignKeyConstraint)
    }


def check_sql(model: type[object]) -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint)
    }


def indexes(model: type[object]) -> dict[str, Index]:
    return {index.name: index for index in model.__table__.indexes}  # type: ignore[attr-defined]


def index_columns(index: Index) -> list[str]:
    return [column.name for column in index.columns if isinstance(column, Column)]


def assert_uuid_primary_key(model: type[object], column_name: str = "id") -> None:
    column = model.__table__.c[column_name]  # type: ignore[attr-defined]

    assert isinstance(column.type, PostgreSQLUUID)
    assert column.primary_key is True
    assert column.nullable is False


def test_entities_table_columns_and_uuid_pk_are_exact() -> None:
    assert Entity.__tablename__ == "entities"
    assert column_names(Entity) == [
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
    assert_uuid_primary_key(Entity)


def test_entities_types_nullability_and_fk_policy() -> None:
    columns = Entity.__table__.c
    fks = foreign_keys(Entity)

    assert fks["fk_entities_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_entities_dataset_id_datasets"].ondelete == "RESTRICT"
    assert columns.generation.type.python_type is int
    assert columns.aliases.type.compile(dialect=postgresql.dialect()) == "TEXT[]"
    assert isinstance(columns.properties.type, JSONB)
    assert isinstance(columns.embedding.type, Vector)
    assert columns.embedding.type.dim == EMBEDDING_DIMENSIONS == 3072
    assert columns.embedding.nullable is True
    assert isinstance(columns.is_active.type, Boolean)
    assert columns.created_at.type.timezone is True
    assert columns.updated_at.type.timezone is True
    assert all(not column.nullable for column in columns if column.name != "embedding")


def test_entities_checks_and_partial_unique_are_stable() -> None:
    checks = check_sql(Entity)
    model_indexes = indexes(Entity)
    partial_unique = model_indexes["uq_entities_dataset_id_canonical_key_active"]

    assert checks["ck_entities_generation_non_negative"] == "generation >= 0"
    assert checks["ck_entities_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert partial_unique.unique is True
    assert index_columns(partial_unique) == ["dataset_id", "canonical_key"]
    assert str(partial_unique.dialect_options["postgresql"]["where"]) == "is_active IS TRUE"


def test_entity_mentions_columns_pk_fks_offsets_and_indexes_are_exact() -> None:
    assert EntityMention.__tablename__ == "entity_mentions"
    assert column_names(EntityMention) == [
        "id",
        "entity_id",
        "chunk_id",
        "surface_text",
        "start_char",
        "end_char",
        "confidence",
    ]
    assert_uuid_primary_key(EntityMention)

    columns = EntityMention.__table__.c
    fks = foreign_keys(EntityMention)
    checks = check_sql(EntityMention)
    model_indexes = indexes(EntityMention)

    assert fks["fk_entity_mentions_entity_id_entities"].elements[0].target_fullname == "entities.id"
    assert fks["fk_entity_mentions_entity_id_entities"].ondelete == "CASCADE"
    assert fks["fk_entity_mentions_chunk_id_chunks"].elements[0].target_fullname == "chunks.id"
    assert fks["fk_entity_mentions_chunk_id_chunks"].ondelete == "CASCADE"
    assert columns.start_char.nullable is True
    assert columns.end_char.nullable is True
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
    assert set(model_indexes) == {"ix_entity_mentions_chunk_id", "ix_entity_mentions_entity_id"}


def test_relations_columns_types_fks_and_indexes_are_exact() -> None:
    assert Relation.__tablename__ == "relations"
    assert column_names(Relation) == [
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
    assert_uuid_primary_key(Relation)

    columns = Relation.__table__.c
    fks = foreign_keys(Relation)
    model_indexes = indexes(Relation)

    assert fks["fk_relations_dataset_id_datasets"].ondelete == "RESTRICT"
    assert (
        fks["fk_relations_source_entity_id_entities"].elements[0].target_fullname == "entities.id"
    )
    assert fks["fk_relations_source_entity_id_entities"].ondelete == "RESTRICT"
    assert (
        fks["fk_relations_target_entity_id_entities"].elements[0].target_fullname == "entities.id"
    )
    assert fks["fk_relations_target_entity_id_entities"].ondelete == "RESTRICT"
    assert isinstance(columns.properties.type, JSONB)
    assert isinstance(columns.embedding.type, Vector)
    assert columns.embedding.type.dim == EMBEDDING_DIMENSIONS == 3072
    assert columns.embedding.nullable is True
    assert isinstance(columns.is_active.type, Boolean)
    assert columns.created_at.type.timezone is True
    assert columns.updated_at.type.timezone is True
    assert set(model_indexes) == {
        "ix_relations_dataset_id_is_active",
        "ix_relations_source_entity_id",
        "ix_relations_target_entity_id",
    }
    assert index_columns(model_indexes["ix_relations_dataset_id_is_active"]) == [
        "dataset_id",
        "is_active",
    ]
    assert "ix_relations_predicate" not in model_indexes


def test_relations_checks_do_not_forbid_self_relations_or_create_ann() -> None:
    checks = check_sql(Relation)

    assert checks["ck_relations_generation_non_negative"] == "generation >= 0"
    assert checks["ck_relations_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert all("source_entity_id !=" not in sql for sql in checks.values())
    assert not any("hnsw" in index.name.lower() for index in indexes(Relation).values())


def test_relation_evidence_columns_composite_pk_fks_and_checks_are_exact() -> None:
    assert RelationEvidence.__tablename__ == "relation_evidence"
    assert column_names(RelationEvidence) == ["relation_id", "chunk_id", "quote", "confidence"]

    columns = RelationEvidence.__table__.c
    fks = foreign_keys(RelationEvidence)
    checks = check_sql(RelationEvidence)
    pk_columns = [column.name for column in RelationEvidence.__table__.primary_key.columns]

    assert pk_columns == ["relation_id", "chunk_id"]
    assert isinstance(columns.relation_id.type, PostgreSQLUUID)
    assert isinstance(columns.chunk_id.type, PostgreSQLUUID)
    assert fks["fk_relation_evidence_relation_id_relations"].ondelete == "CASCADE"
    assert fks["fk_relation_evidence_chunk_id_chunks"].ondelete == "RESTRICT"
    assert columns.quote.nullable is False
    assert checks["ck_relation_evidence_confidence_between_zero_and_one"] == (
        "confidence >= 0 AND confidence <= 1"
    )
    assert set(indexes(RelationEvidence)) == {"ix_relation_evidence_chunk_id"}
    assert index_columns(indexes(RelationEvidence)["ix_relation_evidence_chunk_id"]) == ["chunk_id"]
    assert "ix_relation_evidence_relation_id" not in indexes(RelationEvidence)


def test_no_forbidden_or_sm_210_columns_are_present() -> None:
    forbidden = {
        "deleted_at",
        "document_id",
        "external_id",
        "metadata",
        "neo4j_id",
        "owner_id",
        "source_id",
        "tenant_id",
        "user_id",
    }

    assert {"source_id", "document_id", "metadata"}.isdisjoint(column_names(Entity))
    assert {"dataset_id", "generation", "source_id", "document_id", "metadata"}.isdisjoint(
        column_names(EntityMention)
    )
    assert {"source_id", "document_id", "metadata"}.isdisjoint(column_names(Relation))
    assert forbidden.isdisjoint(column_names(RelationEvidence))
