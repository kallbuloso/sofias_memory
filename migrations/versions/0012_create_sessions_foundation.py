"""Create first-class durable Sessions persistence foundation (ADR-0012, SM-601).

Purely additive: ``sessions``, ``session_entries``, and nullable Session
relationships on ``queries``/``pipeline_runs``. No backfill of any kind is
performed -- legacy ``MemoryEntry.session_id``, ``Document.metadata`` and
``PipelineRun.input`` textual session identifiers remain historical and are
never converted into first-class Session rows or FKs.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-04 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SESSION_ID_MAX_LENGTH = 255

session_status = postgresql.ENUM(
    "active",
    "archived",
    name="session_status",
    create_type=False,
)


def upgrade() -> None:
    session_status.create(op.get_bind())

    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column(
            "status",
            session_status,
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(btrim(key)) > 0", name=op.f("ck_sessions_key_not_blank")),
        sa.CheckConstraint(
            f"char_length(key) <= {SESSION_ID_MAX_LENGTH}",
            name=op.f("ck_sessions_key_max_length"),
        ),
        sa.CheckConstraint(
            "name IS NULL OR char_length(name) <= 120",
            name=op.f("ck_sessions_name_max_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("key", name=op.f("uq_sessions_key")),
    )

    op.create_table(
        "session_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=True),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_session_entries_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "external_id IS NULL OR length(btrim(external_id)) > 0",
            name=op.f("ck_session_entries_external_id_not_blank"),
        ),
        sa.CheckConstraint(
            f"external_id IS NULL OR char_length(external_id) <= {SESSION_ID_MAX_LENGTH}",
            name=op.f("ck_session_entries_external_id_max_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session_entries")),
    )
    # Supports `WHERE session_id = ? ORDER BY created_at, id` (Feature
    # Contract SS 9 entry listing) without a separate turn counter/sequence.
    op.create_index(
        "ix_session_entries_session_id_created_at_id",
        "session_entries",
        ["session_id", "created_at", "id"],
    )
    # Retry-safe append: a caller-supplied external_id is unique within its
    # own Session (but may repeat across different Sessions). Partial index
    # because uniqueness only applies when external_id IS NOT NULL.
    op.create_index(
        "uq_session_entries_session_id_external_id",
        "session_entries",
        ["session_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )

    op.add_column(
        "queries",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "queries",
        sa.Column(
            "session_context_entry_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
    )
    op.create_foreign_key(
        op.f("fk_queries_session_id_sessions"),
        "queries",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_queries_session_id_created_at",
        "queries",
        ["session_id", "created_at"],
    )

    op.add_column(
        "pipeline_runs",
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_pipeline_runs_session_id_sessions"),
        "pipeline_runs",
        "sessions",
        ["session_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        op.f("ix_pipeline_runs_session_id"),
        "pipeline_runs",
        ["session_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_pipeline_runs_session_id"), table_name="pipeline_runs")
    op.drop_constraint(
        op.f("fk_pipeline_runs_session_id_sessions"),
        "pipeline_runs",
        type_="foreignkey",
    )
    op.drop_column("pipeline_runs", "session_id")

    op.drop_index("ix_queries_session_id_created_at", table_name="queries")
    op.drop_constraint(op.f("fk_queries_session_id_sessions"), "queries", type_="foreignkey")
    op.drop_column("queries", "session_context_entry_ids")
    op.drop_column("queries", "session_id")

    op.drop_index(
        "uq_session_entries_session_id_external_id",
        table_name="session_entries",
    )
    op.drop_index("ix_session_entries_session_id_created_at_id", table_name="session_entries")
    op.drop_table("session_entries")

    op.drop_table("sessions")

    session_status.drop(op.get_bind())
