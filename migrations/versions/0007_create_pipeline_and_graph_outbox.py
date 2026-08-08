"""Create pipeline run, step, and graph outbox tables.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHA256_HEX_PATTERN = "'^[0-9a-fA-F]{64}$'"

pipeline_type = postgresql.ENUM(
    "remember",
    "cognify",
    "improve",
    "forget",
    name="pipeline_type",
    create_type=False,
)
pipeline_run_status = postgresql.ENUM(
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelling",
    "cancelled",
    name="pipeline_run_status",
    create_type=False,
)
pipeline_step_status = postgresql.ENUM(
    "queued",
    "running",
    "succeeded",
    "failed",
    "cancelling",
    "cancelled",
    name="pipeline_step_status",
    create_type=False,
)
graph_outbox_operation = postgresql.ENUM(
    "upsert",
    "delete",
    name="graph_outbox_operation",
    create_type=False,
)
graph_outbox_status = postgresql.ENUM(
    "pending",
    "processing",
    "done",
    "failed",
    name="graph_outbox_status",
    create_type=False,
)


def upgrade() -> None:
    pipeline_type.create(op.get_bind())
    pipeline_run_status.create(op.get_bind())
    pipeline_step_status.create(op.get_bind())
    graph_outbox_operation.create(op.get_bind())
    graph_outbox_status.create(op.get_bind())

    op.create_table(
        "pipeline_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pipeline_type", pipeline_type, nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", pipeline_run_status, nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("payload_hash", sa.CHAR(64), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False),
        sa.Column("progress", sa.REAL(), nullable=False),
        sa.Column("current_step", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("config_fingerprint", sa.CHAR(64), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"payload_hash ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_pipeline_runs_payload_hash_hex"),
        ),
        sa.CheckConstraint(
            f"config_fingerprint ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_pipeline_runs_config_fingerprint_hex"),
        ),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_pipeline_runs_attempt_non_negative")),
        sa.ForeignKeyConstraint(
            ["dataset_id"],
            ["datasets.id"],
            name=op.f("fk_pipeline_runs_dataset_id_datasets"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_id"],
            ["sources.id"],
            name=op.f("fk_pipeline_runs_source_id_sources"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_runs")),
    )
    op.create_index(op.f("ix_pipeline_runs_status"), "pipeline_runs", ["status"])
    op.create_index(
        op.f("ix_pipeline_runs_dataset_id_status"),
        "pipeline_runs",
        ["dataset_id", "status"],
    )
    op.create_index(op.f("ix_pipeline_runs_heartbeat_at"), "pipeline_runs", ["heartbeat_at"])
    op.create_index(
        "uq_pipeline_runs_idempotency_key",
        "pipeline_runs",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(op.f("ix_pipeline_runs_created_at"), "pipeline_runs", ["created_at"])

    op.create_table(
        "pipeline_steps",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("status", pipeline_step_status, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_hash", sa.CHAR(64), nullable=True),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_pipeline_steps_attempt_non_negative")),
        sa.CheckConstraint(
            f"input_hash IS NULL OR input_hash ~ {SHA256_HEX_PATTERN}",
            name=op.f("ck_pipeline_steps_input_hash_hex"),
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runs.id"],
            name=op.f("fk_pipeline_steps_run_id_pipeline_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_pipeline_steps")),
    )
    op.create_index(op.f("ix_pipeline_steps_run_id"), "pipeline_steps", ["run_id"])
    op.create_index(
        op.f("ix_pipeline_steps_run_id_ordinal"),
        "pipeline_steps",
        ["run_id", "ordinal"],
    )
    op.create_index(op.f("ix_pipeline_steps_status"), "pipeline_steps", ["status"])

    op.create_table(
        "graph_outbox",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.Text(), nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation", graph_outbox_operation, nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", graph_outbox_status, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 0", name=op.f("ck_graph_outbox_attempt_non_negative")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_outbox")),
    )
    op.create_index(op.f("ix_graph_outbox_status"), "graph_outbox", ["status"])
    op.create_index(
        op.f("ix_graph_outbox_status_created_at"),
        "graph_outbox",
        ["status", "created_at"],
    )
    op.create_index(op.f("ix_graph_outbox_dataset_id"), "graph_outbox", ["dataset_id"])
    op.create_index(
        op.f("ix_graph_outbox_aggregate"),
        "graph_outbox",
        ["aggregate_type", "aggregate_id"],
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_graph_outbox_aggregate"), table_name="graph_outbox")
    op.drop_index(op.f("ix_graph_outbox_dataset_id"), table_name="graph_outbox")
    op.drop_index(op.f("ix_graph_outbox_status_created_at"), table_name="graph_outbox")
    op.drop_index(op.f("ix_graph_outbox_status"), table_name="graph_outbox")
    op.drop_table("graph_outbox")
    op.drop_index(op.f("ix_pipeline_steps_status"), table_name="pipeline_steps")
    op.drop_index(op.f("ix_pipeline_steps_run_id_ordinal"), table_name="pipeline_steps")
    op.drop_index(op.f("ix_pipeline_steps_run_id"), table_name="pipeline_steps")
    op.drop_table("pipeline_steps")
    op.drop_index(op.f("ix_pipeline_runs_created_at"), table_name="pipeline_runs")
    op.drop_index("uq_pipeline_runs_idempotency_key", table_name="pipeline_runs")
    op.drop_index(op.f("ix_pipeline_runs_heartbeat_at"), table_name="pipeline_runs")
    op.drop_index(op.f("ix_pipeline_runs_dataset_id_status"), table_name="pipeline_runs")
    op.drop_index(op.f("ix_pipeline_runs_status"), table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    graph_outbox_status.drop(op.get_bind())
    graph_outbox_operation.drop(op.get_bind())
    pipeline_step_status.drop(op.get_bind())
    pipeline_run_status.drop(op.get_bind())
    pipeline_type.drop(op.get_bind())
