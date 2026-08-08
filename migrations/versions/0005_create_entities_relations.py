"""Create entities, mentions, relations, and evidence tables.

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIMENSIONS = 3072
CONFIDENCE_CHECK_SQL = "confidence >= 0 AND confidence <= 1"


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("canonical_key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("importance_weight", sa.REAL(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("generation >= 0", name=op.f("ck_entities_generation_non_negative")),
        sa.CheckConstraint(
            CONFIDENCE_CHECK_SQL,
            name=op.f("ck_entities_confidence_between_zero_and_one"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_entities_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entities")),
    )
    op.create_index(
        "uq_entities_dataset_id_canonical_key_active",
        "entities",
        ["dataset_id", "canonical_key"],
        unique=True,
        postgresql_where=sa.text("is_active IS TRUE"),
    )

    op.create_table(
        "entity_mentions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("surface_text", sa.Text(), nullable=False),
        sa.Column("start_char", sa.Integer(), nullable=True),
        sa.Column("end_char", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.CheckConstraint(
            "start_char IS NULL OR start_char >= 0",
            name=op.f("ck_entity_mentions_start_char_non_negative"),
        ),
        sa.CheckConstraint(
            "end_char IS NULL OR end_char >= 0",
            name=op.f("ck_entity_mentions_end_char_non_negative"),
        ),
        sa.CheckConstraint(
            "start_char IS NULL OR end_char IS NULL OR end_char >= start_char",
            name=op.f("ck_entity_mentions_char_offsets_valid"),
        ),
        sa.CheckConstraint(
            CONFIDENCE_CHECK_SQL,
            name=op.f("ck_entity_mentions_confidence_between_zero_and_one"),
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
            name=op.f("fk_entity_mentions_entity_id_entities"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name=op.f("fk_entity_mentions_chunk_id_chunks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_entity_mentions")),
    )
    op.create_index(op.f("ix_entity_mentions_entity_id"), "entity_mentions", ["entity_id"])
    op.create_index(op.f("ix_entity_mentions_chunk_id"), "entity_mentions", ["chunk_id"])

    op.create_table(
        "relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("source_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("properties", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.Column("importance_weight", sa.REAL(), nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
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
        sa.CheckConstraint("generation >= 0", name=op.f("ck_relations_generation_non_negative")),
        sa.CheckConstraint(
            CONFIDENCE_CHECK_SQL,
            name=op.f("ck_relations_confidence_between_zero_and_one"),
        ),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_relations_dataset_id_datasets"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_entity_id"],
            ["entities.id"],
            name=op.f("fk_relations_source_entity_id_entities"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["target_entity_id"],
            ["entities.id"],
            name=op.f("fk_relations_target_entity_id_entities"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_relations")),
    )
    op.create_index(
        op.f("ix_relations_dataset_id_is_active"), "relations", ["dataset_id", "is_active"]
    )
    op.create_index(op.f("ix_relations_source_entity_id"), "relations", ["source_entity_id"])
    op.create_index(op.f("ix_relations_target_entity_id"), "relations", ["target_entity_id"])

    op.create_table(
        "relation_evidence",
        sa.Column("relation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=False),
        sa.CheckConstraint(
            CONFIDENCE_CHECK_SQL,
            name=op.f("ck_relation_evidence_confidence_between_zero_and_one"),
        ),
        sa.ForeignKeyConstraint(
            ["relation_id"],
            ["relations.id"],
            name=op.f("fk_relation_evidence_relation_id_relations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name=op.f("fk_relation_evidence_chunk_id_chunks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("relation_id", "chunk_id", name=op.f("pk_relation_evidence")),
    )
    op.create_index(op.f("ix_relation_evidence_chunk_id"), "relation_evidence", ["chunk_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_relation_evidence_chunk_id"), table_name="relation_evidence")
    op.drop_table("relation_evidence")
    op.drop_index(op.f("ix_relations_target_entity_id"), table_name="relations")
    op.drop_index(op.f("ix_relations_source_entity_id"), table_name="relations")
    op.drop_index(op.f("ix_relations_dataset_id_is_active"), table_name="relations")
    op.drop_table("relations")
    op.drop_index(op.f("ix_entity_mentions_chunk_id"), table_name="entity_mentions")
    op.drop_index(op.f("ix_entity_mentions_entity_id"), table_name="entity_mentions")
    op.drop_table("entity_mentions")
    op.drop_index("uq_entities_dataset_id_canonical_key_active", table_name="entities")
    op.drop_table("entities")
