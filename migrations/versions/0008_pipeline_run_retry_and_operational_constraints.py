"""Add PipelineRun retry/lineage fields and operational lifecycle constraints.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-21 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "pipeline_runs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "pipeline_runs",
        sa.Column("retry_of_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        op.f("fk_pipeline_runs_retry_of_run_id_pipeline_runs"),
        "pipeline_runs",
        "pipeline_runs",
        ["retry_of_run_id"],
        ["id"],
        ondelete="SET NULL",
    )

    # ADR-0009 SS K: claim eligibility predicate is `status = 'queued' AND
    # (next_attempt_at IS NULL OR next_attempt_at <= now())`.
    op.create_index(
        "ix_pipeline_runs_status_next_attempt_at",
        "pipeline_runs",
        ["status", "next_attempt_at"],
    )
    # ADR-0009 SS M: manual-retry lineage lookups (chain traversal, audit).
    op.create_index(
        "ix_pipeline_runs_retry_of_run_id",
        "pipeline_runs",
        ["retry_of_run_id"],
    )

    # ADR-0009 SS D freezes a partial unique index as the defense-in-depth
    # backstop for same-dataset serialization -- at most one operational
    # (RUNNING or CANCELLING) write PipelineRun per non-null dataset_id. Its
    # *physical activation* is deliberately DEFERRED past this migration:
    # B4's still-synchronous public write pipelines (Forget in particular)
    # create PipelineRun rows directly as RUNNING and rely on an
    # application-level conflict check that runs *after* that insert, which
    # this constraint would reject before the app ever gets to run its own
    # check. This is a rollout-sequencing decision, not a change to the ADR's
    # final invariant: the constraint is added by a follow-up migration once
    # the last direct-RUNNING B4 writer is migrated to the B5 runtime
    # (tracked as a required cutover step after SM-513, verified at GATE-B5).
    # See docs/exec-plans/active/Sofias_Memory_Technical_Backlog_B5.md SM-502
    # and SM-513 for the recorded decision.

    # ADR-0009 SS B: PipelineStep is one row per (run_id, ordinal). The
    # existing index only expressed this as a query-planning hint; enforce it
    # as a real invariant.
    op.drop_index("ix_pipeline_steps_run_id_ordinal", table_name="pipeline_steps")
    op.create_index(
        "uq_pipeline_steps_run_id_ordinal",
        "pipeline_steps",
        ["run_id", "ordinal"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_pipeline_steps_run_id_ordinal", table_name="pipeline_steps")
    op.create_index(
        "ix_pipeline_steps_run_id_ordinal",
        "pipeline_steps",
        ["run_id", "ordinal"],
    )

    op.drop_index("ix_pipeline_runs_retry_of_run_id", table_name="pipeline_runs")
    op.drop_index("ix_pipeline_runs_status_next_attempt_at", table_name="pipeline_runs")

    op.drop_constraint(
        op.f("fk_pipeline_runs_retry_of_run_id_pipeline_runs"),
        "pipeline_runs",
        type_="foreignkey",
    )
    op.drop_column("pipeline_runs", "retry_of_run_id")
    op.drop_column("pipeline_runs", "next_attempt_at")
