"""Create ContextCheckpoints for logical Context Streams (SM-CTX-002,
ADR-0017/ADR-0018, Contract v1 Contextual Project Memory SS 9/23-35/66-67).

Purely additive: ``context_checkpoints``, ``context_checkpoint_statements``,
``context_checkpoint_provenance`` and the ``context_checkpoint_lifecycle`` /
``context_statement_type`` enums. No existing table is touched; no
``projects``/``context_streams`` table (a Context Stream is the logical pair
``(scope, context_key)``), no Evidence table, no embedding column, no Neo4j
projection, no ``graph_outbox`` event type, no PipelineRun/PipelineStep row.

``context_checkpoint_provenance.origin_kind`` reuses the existing
``cognitive_memory_origin_kind`` enum (created by 0018) and the shared
``contextual_memory_idempotency`` ledger (0019) already carries the
``checkpoint_*`` operations -- neither is created nor dropped here.

Structural invariants:

- ``uq_context_checkpoints_active_stream``: partial unique index
  ``(scope, context_key) WHERE lifecycle = 'active'`` -- at most one ACTIVE
  checkpoint per Context Stream, enforced by PostgreSQL.
- ``uq_context_checkpoints_scope_context_key_revision``: revision is unique per
  stream; FORGOTTEN tombstones keep ``scope``/``context_key``/``revision`` (all
  NOT NULL for every lifecycle) so they keep participating in it.
- ``superseded`` and ``superseded_at``/``superseded_by`` exist as schema readiness
  for SM-CTX-003 only; ``superseded_by`` is ``ON DELETE RESTRICT``.
- statements: ``UNIQUE(checkpoint_id, statement_type, ordinal)`` plus
  ``ck_..._scalar_ordinal_zero`` (scalar types only at ordinal 0) together make
  ``objective``/``current_state``/``next_action`` at most one per checkpoint.

Downgrade drops exactly what this revision created; it destroys any
checkpoint data, which is inherent to reverting this additive revision.

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-21 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCOPE_GRAMMAR = "'^project:[a-z0-9][a-z0-9._-]{0,127}$'"
CONTEXT_KEY_GRAMMAR = "'^[a-z0-9][a-z0-9._:-]{0,127}$'"

context_checkpoint_lifecycle = postgresql.ENUM(
    "active",
    "superseded",
    "invalidated",
    "forgotten",
    name="context_checkpoint_lifecycle",
    create_type=False,
)
context_statement_type = postgresql.ENUM(
    "objective",
    "current_state",
    "approved_decision",
    "pending_work",
    "open_item",
    "blocker",
    "next_action",
    name="context_statement_type",
    create_type=False,
)
cognitive_memory_origin_kind = postgresql.ENUM(
    "user_asserted",
    "tool_observed",
    "imported",
    "inferred",
    "assistant_generated",
    name="cognitive_memory_origin_kind",
    create_type=False,
)
"""Existing enum created by 0018 -- referenced, never created/dropped here."""


def upgrade() -> None:
    bind = op.get_bind()
    context_checkpoint_lifecycle.create(bind)
    context_statement_type.create(bind)

    op.create_table(
        "context_checkpoints",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("context_key", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "lifecycle", context_checkpoint_lifecycle, server_default="active", nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
        sa.Column("forgotten_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["superseded_by"],
            ["context_checkpoints.id"],
            name=op.f("fk_context_checkpoints_superseded_by_context_checkpoints"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"scope ~ {SCOPE_GRAMMAR}", name=op.f("ck_context_checkpoints_scope_grammar")
        ),
        sa.CheckConstraint(
            f"context_key ~ {CONTEXT_KEY_GRAMMAR}",
            name=op.f("ck_context_checkpoints_context_key_grammar"),
        ),
        sa.CheckConstraint("revision >= 1", name=op.f("ck_context_checkpoints_revision_positive")),
        sa.CheckConstraint(
            r"invalidation_reason IS NULL OR invalidation_reason ~ '\S'",
            name=op.f("ck_context_checkpoints_invalidation_reason_not_blank"),
        ),
        sa.CheckConstraint(
            "invalidation_reason IS NULL OR invalidated_at IS NOT NULL",
            name=op.f("ck_context_checkpoints_reason_requires_invalidated_at"),
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL) = (superseded_by IS NULL)",
            name=op.f("ck_context_checkpoints_supersession_markers_paired"),
        ),
        sa.CheckConstraint(
            "superseded_by IS NULL OR superseded_by <> id",
            name=op.f("ck_context_checkpoints_no_self_supersession"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'active' "
            "OR (superseded_at IS NULL AND superseded_by IS NULL AND invalidated_at IS NULL "
            "AND invalidation_reason IS NULL AND forgotten_at IS NULL)",
            name=op.f("ck_context_checkpoints_active_requires_clean_markers"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'superseded' "
            "OR (superseded_at IS NOT NULL AND superseded_by IS NOT NULL "
            "AND invalidated_at IS NULL AND forgotten_at IS NULL)",
            name=op.f("ck_context_checkpoints_superseded_requires_markers"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'invalidated' "
            "OR (invalidated_at IS NOT NULL AND invalidation_reason IS NOT NULL "
            "AND forgotten_at IS NULL)",
            name=op.f("ck_context_checkpoints_invalidated_requires_markers"),
        ),
        sa.CheckConstraint(
            "lifecycle <> 'forgotten' "
            "OR (forgotten_at IS NOT NULL AND invalidation_reason IS NULL)",
            name=op.f("ck_context_checkpoints_forgotten_requires_tombstone_shape"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_checkpoints")),
        sa.UniqueConstraint(
            "scope",
            "context_key",
            "revision",
            name="uq_context_checkpoints_scope_context_key_revision",
        ),
    )
    op.create_index(
        "uq_context_checkpoints_active_stream",
        "context_checkpoints",
        ["scope", "context_key"],
        unique=True,
        postgresql_where=sa.text("lifecycle = 'active'"),
    )
    op.create_index(
        "ix_context_checkpoints_scope_context_key_lifecycle",
        "context_checkpoints",
        ["scope", "context_key", "lifecycle"],
    )
    op.create_index(
        "ix_context_checkpoints_scope_lifecycle_listing",
        "context_checkpoints",
        ["scope", "lifecycle", "context_key", sa.text("revision DESC"), "id"],
    )

    op.create_table(
        "context_checkpoint_statements",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("statement_type", context_statement_type, nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["checkpoint_id"],
            ["context_checkpoints.id"],
            name=op.f("fk_context_checkpoint_statements_checkpoint_id"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            r"content ~ '\S'", name=op.f("ck_context_checkpoint_statements_content_not_blank")
        ),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_context_checkpoint_statements_ordinal_non_negative")
        ),
        sa.CheckConstraint(
            "statement_type NOT IN ('objective', 'current_state', 'next_action') OR ordinal = 0",
            name=op.f("ck_context_checkpoint_statements_scalar_ordinal_zero"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_context_checkpoint_statements")),
        sa.UniqueConstraint(
            "checkpoint_id",
            "statement_type",
            "ordinal",
            name="uq_context_checkpoint_statements_checkpoint_id_type_ordinal",
        ),
    )

    op.create_table(
        "context_checkpoint_provenance",
        sa.Column("checkpoint_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("origin_kind", cognitive_memory_origin_kind, nullable=False),
        sa.Column("source_system", sa.Text(), nullable=False),
        sa.Column("conversation_ref", sa.Text(), nullable=True),
        sa.Column("turn_ref", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["checkpoint_id"],
            ["context_checkpoints.id"],
            name=op.f("fk_context_checkpoint_provenance_checkpoint_id"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            r"source_system ~ '\S'",
            name=op.f("ck_context_checkpoint_provenance_source_system_not_blank"),
        ),
        sa.CheckConstraint(
            r"conversation_ref IS NULL OR conversation_ref ~ '\S'",
            name=op.f("ck_context_checkpoint_provenance_conversation_ref_not_blank"),
        ),
        sa.CheckConstraint(
            r"turn_ref IS NULL OR turn_ref ~ '\S'",
            name=op.f("ck_context_checkpoint_provenance_turn_ref_not_blank"),
        ),
        sa.PrimaryKeyConstraint("checkpoint_id", name=op.f("pk_context_checkpoint_provenance")),
    )


def downgrade() -> None:
    op.drop_table("context_checkpoint_provenance")
    op.drop_table("context_checkpoint_statements")
    op.drop_index(
        "ix_context_checkpoints_scope_lifecycle_listing", table_name="context_checkpoints"
    )
    op.drop_index(
        "ix_context_checkpoints_scope_context_key_lifecycle", table_name="context_checkpoints"
    )
    op.drop_index("uq_context_checkpoints_active_stream", table_name="context_checkpoints")
    op.drop_table("context_checkpoints")

    bind = op.get_bind()
    context_statement_type.drop(bind)
    context_checkpoint_lifecycle.drop(bind)
