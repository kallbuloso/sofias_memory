from __future__ import annotations

from pgvector.sqlalchemy import Vector
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, SmallInteger
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.schema import CreateIndex
from sqlalchemy.sql.schema import Column

from sofias_memory.infrastructure.postgres import Feedback, MemoryEntry, Query, Summary
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


def test_summaries_columns_types_fk_and_target_are_exact() -> None:
    assert Summary.__tablename__ == "summaries"
    assert column_names(Summary) == [
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

    columns = Summary.__table__.c
    fks = foreign_keys(Summary)

    assert isinstance(columns.id.type, PostgreSQLUUID)
    assert columns.id.primary_key is True
    assert fks["fk_summaries_dataset_id_datasets"].elements[0].target_fullname == "datasets.id"
    assert fks["fk_summaries_dataset_id_datasets"].ondelete == "RESTRICT"
    assert columns.generation.type.python_type is int
    assert isinstance(columns.target_type.type, ENUM)
    assert columns.target_type.type.name == "summary_target_type"
    assert columns.target_type.type.enums == ["document", "entity", "dataset", "cluster"]
    assert columns.target_id.nullable is True
    assert len(columns.target_id.foreign_keys) == 0
    assert columns.level.type.python_type is int
    assert isinstance(columns.embedding.type, Vector)
    assert columns.embedding.type.dim == EMBEDDING_DIMENSIONS == 3072
    assert columns.embedding.nullable is False
    assert columns.created_at.type.timezone is True
    assert all(column.name == "target_id" or not column.nullable for column in columns)


def test_summaries_indexes_preserve_filter_and_adr_0006_ann() -> None:
    model_indexes = indexes(Summary)
    filter_index = model_indexes["ix_summaries_dataset_id_generation_is_active"]
    ann_index = model_indexes["ix_summaries_embedding_halfvec_hnsw"]
    compiled_ann = str(CreateIndex(ann_index).compile(dialect=postgresql.dialect()))

    assert check_sql(Summary)["ck_summaries_generation_non_negative"] == "generation >= 0"
    assert set(model_indexes) == {
        "ix_summaries_dataset_id_generation_is_active",
        "ix_summaries_embedding_halfvec_hnsw",
    }
    assert index_columns(filter_index) == ["dataset_id", "generation", "is_active"]
    assert ann_index.dialect_options["postgresql"]["using"] == "hnsw"
    assert "embedding::halfvec(3072)" in compiled_ann
    assert "halfvec_cosine_ops" in compiled_ann
    assert "ivfflat" not in compiled_ann.lower()
    assert "vector_l2_ops" not in compiled_ann
    assert "vector_ip_ops" not in compiled_ann


def test_memory_entries_columns_fks_metadata_and_indexes_are_exact() -> None:
    assert MemoryEntry.__tablename__ == "memory_entries"
    assert column_names(MemoryEntry) == [
        "id",
        "dataset_id",
        "source_id",
        "session_id",
        "entry_type",
        "content",
        "metadata",
        "created_at",
    ]

    columns = MemoryEntry.__table__.c
    fks = foreign_keys(MemoryEntry)
    model_indexes = indexes(MemoryEntry)

    assert fks["fk_memory_entries_dataset_id_datasets"].ondelete == "RESTRICT"
    assert fks["fk_memory_entries_source_id_sources"].elements[0].target_fullname == "sources.id"
    assert fks["fk_memory_entries_source_id_sources"].ondelete == "SET NULL"
    assert columns.source_id.nullable is True
    assert columns.session_id.nullable is True
    assert isinstance(columns.entry_type.type, ENUM)
    assert columns.entry_type.type.name == "memory_entry_type"
    assert columns.entry_type.type.enums == ["text", "qa", "feedback", "note"]
    assert isinstance(columns["metadata"].type, JSONB)
    assert "metadata_" in MemoryEntry.__mapper__.attrs
    assert MemoryEntry.__mapper__.attrs["metadata_"].columns[0].name == "metadata"
    assert columns.created_at.type.timezone is True
    assert model_indexes["ix_memory_entries_dataset_id"].columns.keys() == ["dataset_id"]
    assert model_indexes["ix_memory_entries_source_id"].columns.keys() == ["source_id"]


def test_queries_columns_nullable_content_and_types_are_exact() -> None:
    assert Query.__tablename__ == "queries"
    assert column_names(Query) == [
        "id",
        "query_text",
        "dataset_ids",
        "mode",
        "answer",
        "references",
        "timings",
        "model",
        "session_id",
        "session_context_entry_ids",
        "created_at",
    ]

    columns = Query.__table__.c

    assert columns.query_text.nullable is True
    assert columns.answer.nullable is True
    assert columns.model.nullable is True
    assert columns.dataset_ids.type.compile(dialect=postgresql.dialect()) == "UUID[]"
    assert len(columns.dataset_ids.foreign_keys) == 0
    assert columns.mode.type.python_type is str
    assert isinstance(columns["references"].type, JSONB)
    assert isinstance(columns.timings.type, JSONB)
    assert columns.created_at.type.timezone is True
    assert columns.session_id.nullable is True
    assert columns.session_context_entry_ids.nullable is False
    assert columns.session_context_entry_ids.type.compile(dialect=postgresql.dialect()) == (
        "UUID[]"
    )
    assert "query_hash" not in columns
    assert "answer_hash" not in columns

    fks = foreign_keys(Query)
    assert fks["fk_queries_session_id_sessions"].ondelete == "SET NULL"
    query_indexes = indexes(Query)
    assert index_columns(query_indexes["ix_queries_session_id_created_at"]) == [
        "session_id",
        "created_at",
    ]


def test_feedback_columns_fk_score_target_and_index_are_exact() -> None:
    assert Feedback.__tablename__ == "feedback"
    assert column_names(Feedback) == [
        "id",
        "query_id",
        "target_type",
        "target_id",
        "score",
        "comment",
        "applied_at",
        "created_at",
    ]

    columns = Feedback.__table__.c
    fks = foreign_keys(Feedback)
    model_indexes = indexes(Feedback)

    assert fks["fk_feedback_query_id_queries"].elements[0].target_fullname == "queries.id"
    assert fks["fk_feedback_query_id_queries"].ondelete == "RESTRICT"
    assert columns.target_type.type.python_type is str
    assert columns.target_id.nullable is True
    assert len(columns.target_id.foreign_keys) == 0
    assert isinstance(columns.score.type, SmallInteger)
    assert check_sql(Feedback)["ck_feedback_score_allowed_values"] == "score IN (-1, 0, 1)"
    assert columns.comment.nullable is True
    assert columns.applied_at.nullable is True
    assert columns.applied_at.type.timezone is True
    assert columns.created_at.type.timezone is True
    assert model_indexes["ix_feedback_query_id"].columns.keys() == ["query_id"]


def test_no_sm211_or_forbidden_columns_are_present() -> None:
    forbidden = {
        "deleted_at",
        "graph_outbox",
        "owner_id",
        "pipeline_run_id",
        "pipeline_step_id",
        "tenant_id",
        "user_id",
    }

    for model in (Summary, MemoryEntry, Query, Feedback):
        assert forbidden.isdisjoint(column_names(model))
