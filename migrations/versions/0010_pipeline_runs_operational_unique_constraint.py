"""Activate the deferred partial unique operational-run constraint on
pipeline_runs (ADR-0009 SS D, SM-513).

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-24 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SM-513 is the last direct-RUNNING B4 writer's migration (Remember);
    # every public write pipeline now creates PipelineRun rows exclusively
    # through the B5 claimer, so this backstop can finally be activated
    # (deferred since 0008/SM-502, see the ORM model's own history). If any
    # already-persisted RUNNING/CANCELLING pair for the same dataset_id
    # violates this, index creation fails loudly here -- deliberately never
    # silently reconciled by this migration.
    op.create_index(
        "uq_pipeline_runs_dataset_id_operational",
        "pipeline_runs",
        ["dataset_id"],
        unique=True,
        postgresql_where=("dataset_id IS NOT NULL AND status IN ('running', 'cancelling')"),
    )


def downgrade() -> None:
    op.drop_index("uq_pipeline_runs_dataset_id_operational", table_name="pipeline_runs")
