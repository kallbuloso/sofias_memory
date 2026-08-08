"""Create datasets table.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

dataset_status = postgresql.ENUM(
    "active",
    "deleting",
    "deleted",
    name="dataset_status",
    create_type=False,
)


def upgrade() -> None:
    dataset_status.create(op.get_bind())
    op.create_table(
        "datasets",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "status",
            dataset_status,
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "active_generation",
            sa.Integer(),
            server_default=sa.text("0"),
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
        sa.CheckConstraint(
            "length(btrim(name::text)) > 0", name=op.f("ck_datasets_name_not_blank")
        ),
        sa.CheckConstraint(
            "char_length(name::text) <= 120", name=op.f("ck_datasets_name_max_length")
        ),
        sa.CheckConstraint("length(btrim(slug)) > 0", name=op.f("ck_datasets_slug_not_blank")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_datasets")),
        sa.UniqueConstraint("name", name=op.f("uq_datasets_name")),
        sa.UniqueConstraint("slug", name=op.f("uq_datasets_slug")),
    )


def downgrade() -> None:
    op.drop_table("datasets")
    dataset_status.drop(op.get_bind())
