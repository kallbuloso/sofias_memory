"""Create summaries, memory entries, queries, and feedback tables.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 3072
SUMMARIES_ANN_INDEX_SQL = (
    "CREATE INDEX ix_summaries_embedding_halfvec_hnsw "
    "ON summaries USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)"
)

summary_target_type = postgresql.ENUM(
    "document",
    "entity",
    "dataset",
    "cluster",
    name="summary_target_type",
    create_type=False,
)
memory_entry_type = postgresql.ENUM(
    "text",
    "qa",
    "feedback",
    "note",
    name="memory_entry_type",
    create_type=False,
)


def upgrade() -> None:
    summary_target_type.create(op.get_bind())
    memory_entry_type.create(op.get_bind())

    op.create_table(
        "summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("target_type", summary_target_type, nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("generation >= 0", name=op.f("ck_summaries_generation_non_negative")),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_summaries_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_summaries")),
    )
    op.create_index(
        op.f("ix_summaries_dataset_id_generation_is_active"),
        "summaries",
        ["dataset_id", "generation", "is_active"],
    )
    op.execute(SUMMARIES_ANN_INDEX_SQL)

    op.create_table(
        "memory_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", sa.Text(), nullable=True),
        sa.Column("entry_type", memory_entry_type, nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_memory_entries_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_memory_entries_source_id_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_entries")),
    )
    op.create_index(op.f("ix_memory_entries_dataset_id"), "memory_entries", ["dataset_id"])
    op.create_index(op.f("ix_memory_entries_source_id"), "memory_entries", ["source_id"])

    op.create_table(
        "queries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column("dataset_ids", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column("references", postgresql.JSONB(), nullable=False),
        sa.Column("timings", postgresql.JSONB(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_queries")),
    )

    op.create_table(
        "feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("query_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("score IN (-1, 0, 1)", name=op.f("ck_feedback_score_allowed_values")),
        sa.ForeignKeyConstraint(
            ["query_id"],
            ["queries.id"],
            name=op.f("fk_feedback_query_id_queries"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_feedback")),
    )
    op.create_index(op.f("ix_feedback_query_id"), "feedback", ["query_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_query_id"), table_name="feedback")
    op.drop_table("feedback")
    op.drop_table("queries")
    op.drop_index(op.f("ix_memory_entries_source_id"), table_name="memory_entries")
    op.drop_index(op.f("ix_memory_entries_dataset_id"), table_name="memory_entries")
    op.drop_table("memory_entries")
    op.execute("DROP INDEX ix_summaries_embedding_halfvec_hnsw")
    op.drop_index(op.f("ix_summaries_dataset_id_generation_is_active"), table_name="summaries")
    op.drop_table("summaries")
    memory_entry_type.drop(op.get_bind())
    summary_target_type.drop(op.get_bind())
