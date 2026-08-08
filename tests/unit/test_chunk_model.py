from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.schema import Column

from sofias_memory.infrastructure.postgres import Chunk
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS


def column_names() -> list[str]:
    return [column.name for column in Chunk.__table__.columns]


def foreign_keys() -> dict[str, ForeignKeyConstraint]:
    return {
        constraint.name: constraint
        for constraint in Chunk.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def check_sql() -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in Chunk.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def unique_columns() -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in Chunk.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def indexes() -> dict[str, Index]:
    return {index.name: index for index in Chunk.__table__.indexes}


def index_columns(index: Index) -> list[str]:
    return [column.name for column in index.columns if isinstance(column, Column)]


def test_chunk_table_and_columns_are_exact() -> None:
    assert Chunk.__tablename__ == "chunks"
    assert column_names() == [
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


def test_chunk_uuid_and_fk_policies() -> None:
    columns = Chunk.__table__.c
    fks = foreign_keys()

    assert isinstance(columns.id.type, PostgreSQLUUID)
    assert columns.id.primary_key is True
    assert fks["fk_chunks_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_chunks_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_chunks_document_id_documents"].elements[0].target_fullname == "documents.id"
    assert fks["fk_chunks_document_id_documents"].ondelete == "RESTRICT"
    assert fks["fk_chunks_source_id_sources"].elements[0].target_fullname == "sources.id"
    assert fks["fk_chunks_source_id_sources"].ondelete == "RESTRICT"


def test_chunk_scalar_types_and_nullability() -> None:
    columns = Chunk.__table__.c

    assert columns.generation.type.python_type is int
    assert columns.ordinal.type.python_type is int
    assert columns.text.type.python_type is str
    assert columns.content_sha256.type.length == 64
    assert columns.token_count.type.python_type is int
    assert columns.start_char.type.python_type is int
    assert columns.end_char.type.python_type is int
    assert isinstance(columns.is_active.type, Boolean)
    assert columns.created_at.type.timezone is True
    assert "updated_at" not in columns
    assert all(not column.nullable for column in columns)


def test_chunk_section_path_metadata_embedding_and_lexical_types() -> None:
    columns = Chunk.__table__.c

    assert columns.section_path.type.compile(dialect=postgresql.dialect()) == "TEXT[]"
    assert columns.section_path.type.item_type.python_type is str
    assert isinstance(columns["metadata"].type, JSONB)
    assert "metadata_" in Chunk.__mapper__.attrs
    assert Chunk.__mapper__.attrs["metadata_"].columns[0].name == "metadata"
    assert isinstance(columns.embedding.type, Vector)
    assert columns.embedding.type.dim == EMBEDDING_DIMENSIONS == 3072
    assert isinstance(columns.lexical.type, TSVECTOR)


def test_chunk_constraints_are_structural_and_stable() -> None:
    checks = check_sql()

    assert checks["ck_chunks_content_sha256_hex"] == "content_sha256 ~ '^[0-9a-fA-F]{64}$'"
    assert checks["ck_chunks_generation_non_negative"] == "generation >= 0"
    assert checks["ck_chunks_ordinal_non_negative"] == "ordinal >= 0"
    assert checks["ck_chunks_token_count_non_negative"] == "token_count >= 0"
    assert checks["ck_chunks_char_offsets_valid"] == "start_char >= 0 AND end_char >= start_char"
    assert ("document_id", "generation", "ordinal") in unique_columns()


def test_chunk_indexes_are_exact() -> None:
    model_indexes = indexes()

    assert set(model_indexes) == {
        "ix_chunks_dataset_id_is_active",
        "ix_chunks_embedding_halfvec_hnsw",
        "ix_chunks_lexical",
        "ix_chunks_source_id_is_active",
    }
    assert model_indexes["ix_chunks_lexical"].dialect_options["postgresql"]["using"] == "gin"
    assert index_columns(model_indexes["ix_chunks_lexical"]) == ["lexical"]
    assert index_columns(model_indexes["ix_chunks_dataset_id_is_active"]) == [
        "dataset_id",
        "is_active",
    ]
    assert index_columns(model_indexes["ix_chunks_source_id_is_active"]) == [
        "source_id",
        "is_active",
    ]


def test_chunk_ann_index_uses_halfvec_hnsw_cosine_expression() -> None:
    ann_index = indexes()["ix_chunks_embedding_halfvec_hnsw"]
    compiled = str(CreateIndex(ann_index).compile(dialect=postgresql.dialect()))

    assert ann_index.dialect_options["postgresql"]["using"] == "hnsw"
    assert "embedding::halfvec(3072)" in compiled
    assert "halfvec_cosine_ops" in compiled
    assert "vector_l2_ops" not in compiled
    assert "vector_ip_ops" not in compiled
    assert "vector_cosine_ops" not in compiled
    assert "ivfflat" not in compiled.lower()


def test_chunk_has_no_forbidden_or_future_columns() -> None:
    forbidden = {
        "tenant_id",
        "owner_id",
        "user_id",
        "deleted_at",
        "updated_at",
        "embedding_model",
        "embedding_dimensions",
        "neo4j_id",
        "score",
        "rank",
    }

    assert forbidden.isdisjoint(column_names())
