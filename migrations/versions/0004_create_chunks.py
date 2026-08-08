"""Create chunks table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 3072
SHA256_HEX_PATTERN = "'^[0-9a-fA-F]{64}$'"
CHUNKS_ANN_INDEX_SQL = (
    "CREATE INDEX ix_chunks_embedding_halfvec_hnsw "
    "ON chunks USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops)"
)


def upgrade() -> None:
    op.create_table(
        "chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=False),
        sa.Column("end_char", sa.Integer(), nullable=False),
        sa.Column("section_path", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
        sa.Column("lexical", postgresql.TSVECTOR(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"content_sha256 ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_chunks_content_sha256_hex"),
        ),
        sa.CheckConstraint("generation >= 0", name=op.f("ck_chunks_generation_non_negative")),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_chunks_ordinal_non_negative")),
        sa.CheckConstraint("token_count >= 0", name=op.f("ck_chunks_token_count_non_negative")),
        sa.CheckConstraint(
            "start_char >= 0 AND end_char >= start_char",
            name=op.f("ck_chunks_char_offsets_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_chunks_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_chunks_document_id_documents"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_chunks_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint(
            "document_id",
            "generation",
            "ordinal",
            name=op.f("uq_chunks_document_id_generation_ordinal"),
        ),
    )
    op.create_index("ix_chunks_lexical", "chunks", ["lexical"], postgresql_using="gin")
    op.create_index(op.f("ix_chunks_dataset_id_is_active"), "chunks", ["dataset_id", "is_active"])
    op.create_index(op.f("ix_chunks_source_id_is_active"), "chunks", ["source_id", "is_active"])
    op.execute(CHUNKS_ANN_INDEX_SQL)


def downgrade() -> None:
    op.execute("DROP INDEX ix_chunks_embedding_halfvec_hnsw")
    op.drop_index(op.f("ix_chunks_source_id_is_active"), table_name="chunks")
    op.drop_index(op.f("ix_chunks_dataset_id_is_active"), table_name="chunks")
    op.drop_index("ix_chunks_lexical", table_name="chunks")
    op.drop_table("chunks")
