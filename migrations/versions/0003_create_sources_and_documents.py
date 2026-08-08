"""Create sources and documents tables.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

source_kind = postgresql.ENUM("text", "file", "url", name="source_kind", create_type=False)
source_status = postgresql.ENUM(
    "pending",
    "processing",
    "active",
    "failed",
    "deleting",
    "deleted",
    name="source_status",
    create_type=False,
)

SHA256_HEX_PATTERN = "'^[0-9a-fA-F]{64}$'"


def upgrade() -> None:
    source_kind.create(op.get_bind())
    source_status.create(op.get_bind())
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", source_kind, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("original_uri", sa.Text(), nullable=True),
        sa.Column("storage_uri", sa.Text(), nullable=True),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("normalized_sha256", sa.CHAR(64), nullable=True),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("status", source_status, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"content_sha256 ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_sources_content_sha256_hex"),
        ),
        sa.CheckConstraint(
            f"normalized_sha256 IS NULL OR normalized_sha256 ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_sources_normalized_sha256_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_sources_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sources")),
        sa.UniqueConstraint(
            "dataset_id",
            "content_sha256",
            "version",
            name=op.f("uq_sources_dataset_id_content_sha256_version"),
        ),
    )
    op.create_index("ix_sources_metadata", "sources", ["metadata"], postgresql_using="gin")
    op.create_index(op.f("ix_sources_status"), "sources", ["status"])
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.CHAR(64), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            f"text_sha256 ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_documents_text_sha256_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_documents_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_documents_source_id_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_index(
        op.f("ix_documents_dataset_id_generation"),
        "documents",
        ["dataset_id", "generation"],
    )
    op.create_index(
        op.f("ix_documents_source_id_generation"),
        "documents",
        ["source_id", "generation"],
    )
    op.create_index(
        "ix_documents_active_generation",
        "documents",
        ["dataset_id", "generation"],
        postgresql_where=sa.text("is_active IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index("ix_documents_active_generation", table_name="documents")
    op.drop_index(op.f("ix_documents_source_id_generation"), table_name="documents")
    op.drop_index(op.f("ix_documents_dataset_id_generation"), table_name="documents")
    op.drop_table("documents")
    op.drop_index(op.f("ix_sources_status"), table_name="sources")
    op.drop_index("ix_sources_metadata", table_name="sources")
    op.drop_table("sources")
    source_status.drop(op.get_bind())
    source_kind.drop(op.get_bind())
