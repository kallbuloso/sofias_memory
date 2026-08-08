from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID

from sofias_memory.domain import SourceKind, SourceStatus
from sofias_memory.infrastructure.postgres import Document, Source


def source_columns() -> list[str]:
    return [column.name for column in Source.__table__.columns]


def document_columns() -> list[str]:
    return [column.name for column in Document.__table__.columns]


def check_sql(model: type[Source] | type[Document]) -> dict[str | None, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    }


def unique_columns(model: type[Source] | type[Document]) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def indexes(model: type[Source] | type[Document]) -> dict[str, Index]:
    return {index.name: index for index in model.__table__.indexes}


def foreign_keys(model: type[Source] | type[Document]) -> dict[str, ForeignKeyConstraint]:
    return {
        constraint.name: constraint
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
    }


def test_source_table_and_columns_are_exact() -> None:
    assert Source.__tablename__ == "sources"
    assert source_columns() == [
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


def test_source_types_and_nullability() -> None:
    columns = Source.__table__.c

    assert isinstance(columns.id.type, PostgreSQLUUID)
    assert columns.id.primary_key is True
    assert isinstance(columns.kind.type, ENUM)
    assert columns.kind.type.name == "source_kind"
    assert columns.kind.type.enums == ["text", "file", "url"]
    assert columns.kind.nullable is False
    assert columns.original_uri.nullable is True
    assert columns.storage_uri.nullable is True
    assert columns.normalized_sha256.nullable is True
    assert columns.content_sha256.type.length == 64
    assert columns.normalized_sha256.type.length == 64
    assert isinstance(columns.byte_size.type, BigInteger)
    assert isinstance(columns["metadata"].type, JSONB)
    assert isinstance(columns.status.type, ENUM)
    assert columns.status.type.name == "source_status"
    assert columns.status.type.enums == [
        "pending",
        "processing",
        "active",
        "failed",
        "deleting",
        "deleted",
    ]
    assert columns.version.nullable is False
    assert columns.created_at.type.timezone is True
    assert columns.updated_at.type.timezone is True


def test_source_metadata_python_attribute_maps_to_sql_metadata() -> None:
    assert "metadata" in Source.__table__.c
    assert "metadata_" in Source.__mapper__.attrs
    assert Source.__mapper__.attrs["metadata_"].columns[0].name == "metadata"


def test_source_fk_policy_and_constraints() -> None:
    fks = foreign_keys(Source)
    checks = check_sql(Source)

    assert fks["fk_sources_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_sources_dataset_id_datasets"].ondelete == "RESTRICT"
    assert ("dataset_id", "content_sha256", "version") in unique_columns(Source)
    assert checks["ck_sources_content_sha256_hex"] == "content_sha256 ~ '^[0-9a-fA-F]{64}$'"
    assert (
        checks["ck_sources_normalized_sha256_hex"]
        == "normalized_sha256 IS NULL OR normalized_sha256 ~ '^[0-9a-fA-F]{64}$'"
    )


def test_source_indexes_are_exact() -> None:
    model_indexes = indexes(Source)

    assert set(model_indexes) == {"ix_sources_metadata", "ix_sources_status"}
    assert model_indexes["ix_sources_metadata"].dialect_options["postgresql"]["using"] == "gin"
    assert [column.name for column in model_indexes["ix_sources_metadata"].columns] == ["metadata"]
    assert [column.name for column in model_indexes["ix_sources_status"].columns] == ["status"]


def test_document_table_and_columns_are_exact() -> None:
    assert Document.__tablename__ == "documents"
    assert document_columns() == [
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


def test_document_types_and_nullability() -> None:
    columns = Document.__table__.c

    assert isinstance(columns.id.type, PostgreSQLUUID)
    assert columns.id.primary_key is True
    assert columns.generation.type.python_type is int
    assert columns.title.type.python_type is str
    assert columns.language.type.length == 16
    assert columns.normalized_text.type.python_type is str
    assert columns.text_sha256.type.length == 64
    assert columns.token_count.type.python_type is int
    assert isinstance(columns["metadata"].type, JSONB)
    assert isinstance(columns.is_active.type, Boolean)
    assert columns.created_at.type.timezone is True
    assert "updated_at" not in columns


def test_document_metadata_python_attribute_maps_to_sql_metadata() -> None:
    assert "metadata" in Document.__table__.c
    assert "metadata_" in Document.__mapper__.attrs
    assert Document.__mapper__.attrs["metadata_"].columns[0].name == "metadata"


def test_document_fk_policy_and_hash_constraint() -> None:
    fks = foreign_keys(Document)
    checks = check_sql(Document)

    assert fks["fk_documents_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_documents_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_documents_source_id_sources"].elements[0].target_fullname == "sources.id"
    assert fks["fk_documents_source_id_sources"].ondelete == "RESTRICT"
    assert checks["ck_documents_text_sha256_hex"] == "text_sha256 ~ '^[0-9a-fA-F]{64}$'"


def test_document_indexes_are_exact() -> None:
    model_indexes = indexes(Document)

    assert set(model_indexes) == {
        "ix_documents_active_generation",
        "ix_documents_dataset_id_generation",
        "ix_documents_source_id_generation",
    }
    assert [
        column.name for column in model_indexes["ix_documents_dataset_id_generation"].columns
    ] == [
        "dataset_id",
        "generation",
    ]
    assert [
        column.name for column in model_indexes["ix_documents_source_id_generation"].columns
    ] == [
        "source_id",
        "generation",
    ]
    active_index = model_indexes["ix_documents_active_generation"]
    assert [column.name for column in active_index.columns] == ["dataset_id", "generation"]
    assert str(active_index.dialect_options["postgresql"]["where"]) == "is_active IS TRUE"


def test_source_and_document_have_no_forbidden_or_future_columns() -> None:
    forbidden = {
        "tenant_id",
        "owner_id",
        "user_id",
        "deleted_at",
        "embedding",
        "lexical",
        "chunk_count",
        "neo4j_id",
    }

    assert forbidden.isdisjoint(source_columns())
    assert forbidden.isdisjoint(document_columns())
    assert SourceKind.TEXT.value == "text"
    assert SourceStatus.PENDING.value == "pending"
