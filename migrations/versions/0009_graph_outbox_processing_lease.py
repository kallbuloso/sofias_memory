"""Add graph_outbox processing lease fields (ADR-0009 SS V, SM-506).

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-21 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "graph_outbox",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "graph_outbox",
        sa.Column("worker_id", sa.Text(), nullable=True),
    )
    # ADR-0009 SS V: stale-processing recovery predicate reads
    # `status = 'processing' AND processing_started_at < now() - threshold`.
    op.create_index(
        "ix_graph_outbox_status_processing_started_at",
        "graph_outbox",
        ["status", "processing_started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_graph_outbox_status_processing_started_at", table_name="graph_outbox")
    op.drop_column("graph_outbox", "worker_id")
    op.drop_column("graph_outbox", "processing_started_at")
