"""Create first-class contextual Evidence (SM-CTX-004, ADR-0019, Contract v1
Contextual Project Memory SS 11-13, handoff 12 SS 13/29/32/76-79).

Purely additive on top of 0021 (merged migrations are never edited):

- ``evidence_source_type`` (exactly the six v1 types) and ``support_mode``
  (``all_required``/``any_sufficient``) enums.
- ``contextual_evidence_refs``: canonical Evidence identity, one row per
  ``(source_type, source_id)`` (``uq_contextual_evidence_refs_source``), shared by
  every owner. ``source_id`` deliberately has no FK (polymorphic target);
  validity is the EvidenceResolver's job. No availability column: availability
  is always computed from the authoritative source state, never cached.
- ``episode_evidence_refs`` / ``context_statement_evidence_refs``: owner links,
  PK ``(owner, evidence_ref_id)``; owner FK ``ON DELETE CASCADE`` (structural
  safety only -- no public hard delete), Evidence FK ``ON DELETE RESTRICT`` (no
  dangling links). Indexed by ``evidence_ref_id`` for the inbound-dependent
  lookups of SM-CTX-005.
- nullable ``support_mode`` on ``episodes`` and ``context_checkpoint_statements``
  (NULL = no declared support = independent assertion, never "unsupported").
  ``ck_episodes_forgotten_has_no_support``: a FORGOTTEN tombstone carries no
  support (Contract SS 18 tombstone ``"support": null``).

No FK from any source table into these tables: forgetting/deleting a source
never cascades into derived contextual resources (handoff 12 SS 112). No
backfill: pre-SM-CTX-004 Episodes/statements keep ``support_mode = NULL``.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-21 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

evidence_source_type = postgresql.ENUM(
    "session_entry",
    "memory_item",
    "episode",
    "source",
    "document",
    "chunk",
    name="evidence_source_type",
    create_type=False,
)
support_mode = postgresql.ENUM(
    "all_required",
    "any_sufficient",
    name="support_mode",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    evidence_source_type.create(bind)
    support_mode.create(bind)

    op.create_table(
        "contextual_evidence_refs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_type", evidence_source_type, nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contextual_evidence_refs")),
        sa.UniqueConstraint("source_type", "source_id", name="uq_contextual_evidence_refs_source"),
    )

    op.create_table(
        "episode_evidence_refs",
        sa.Column("episode_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_ref_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["episode_id"],
            ["episodes.id"],
            name=op.f("fk_episode_evidence_refs_episode_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref_id"],
            ["contextual_evidence_refs.id"],
            name=op.f("fk_episode_evidence_refs_evidence_ref_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "episode_id", "evidence_ref_id", name=op.f("pk_episode_evidence_refs")
        ),
    )
    op.create_index(
        "ix_episode_evidence_refs_evidence_ref_id", "episode_evidence_refs", ["evidence_ref_id"]
    )

    op.create_table(
        "context_statement_evidence_refs",
        sa.Column("statement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_ref_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["statement_id"],
            ["context_checkpoint_statements.id"],
            name=op.f("fk_context_statement_evidence_refs_statement_id"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_ref_id"],
            ["contextual_evidence_refs.id"],
            name=op.f("fk_context_statement_evidence_refs_evidence_ref_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "statement_id", "evidence_ref_id", name=op.f("pk_context_statement_evidence_refs")
        ),
    )
    op.create_index(
        "ix_context_statement_evidence_refs_evidence_ref_id",
        "context_statement_evidence_refs",
        ["evidence_ref_id"],
    )

    op.add_column("episodes", sa.Column("support_mode", support_mode, nullable=True))
    op.create_check_constraint(
        op.f("ck_episodes_forgotten_has_no_support"),
        "episodes",
        "lifecycle <> 'forgotten' OR support_mode IS NULL",
    )
    op.add_column(
        "context_checkpoint_statements",
        sa.Column("support_mode", support_mode, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("context_checkpoint_statements", "support_mode")
    op.drop_constraint(op.f("ck_episodes_forgotten_has_no_support"), "episodes", type_="check")
    op.drop_column("episodes", "support_mode")
    op.drop_index(
        "ix_context_statement_evidence_refs_evidence_ref_id",
        table_name="context_statement_evidence_refs",
    )
    op.drop_table("context_statement_evidence_refs")
    op.drop_index("ix_episode_evidence_refs_evidence_ref_id", table_name="episode_evidence_refs")
    op.drop_table("episode_evidence_refs")
    op.drop_table("contextual_evidence_refs")

    bind = op.get_bind()
    support_mode.drop(bind)
    evidence_source_type.drop(bind)
