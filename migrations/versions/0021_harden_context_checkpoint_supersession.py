"""Harden ContextCheckpoint supersession lineage (SM-CTX-003, ADR-0018,
Contract v1 Contextual Project Memory SS 31-32, handoff 11 SS 17-21/44/68).

Purely additive/reversible on top of 0020 (which is merged and never edited):

- ``uq_context_checkpoints_superseded_by``: partial unique index on
  ``superseded_by WHERE superseded_by IS NOT NULL`` -- a replacement has at most
  one direct predecessor, so lineage stays linear (handoff 11 SS 17/20).
- ``fk_context_checkpoints_superseded_by_context_checkpoints`` becomes
  ``DEFERRABLE INITIALLY IMMEDIATE``. It stays immediate for every statement and
  transaction by default; only the supersede transaction issues
  ``SET CONSTRAINTS ... DEFERRED`` so that, inside one transaction, the old
  ACTIVE can become SUPERSEDED (whose CHECK requires a non-null
  ``superseded_by``) *before* the replacement ACTIVE is inserted (the
  single-ACTIVE partial unique index is not deferrable). The FK is still
  verified at COMMIT, so a dangling lineage can never be committed. ``ON DELETE
  RESTRICT`` is unchanged.

Same-stream consistency between predecessor and replacement is enforced by
the service inside the transaction (handoff 11 SS 21), not by a trigger.

Downgrade restores the 0020 shape exactly (drops the index, FK back to
``NOT DEFERRABLE``); no data is touched.

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-21 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LINEAGE_FK = "fk_context_checkpoints_superseded_by_context_checkpoints"
SUPERSEDED_BY_UNIQUE_INDEX = "uq_context_checkpoints_superseded_by"


def upgrade() -> None:
    op.create_index(
        SUPERSEDED_BY_UNIQUE_INDEX,
        "context_checkpoints",
        ["superseded_by"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NOT NULL"),
    )
    op.execute(
        f"ALTER TABLE context_checkpoints ALTER CONSTRAINT {LINEAGE_FK} "
        "DEFERRABLE INITIALLY IMMEDIATE"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE context_checkpoints ALTER CONSTRAINT {LINEAGE_FK} NOT DEFERRABLE")
    op.drop_index(SUPERSEDED_BY_UNIQUE_INDEX, table_name="context_checkpoints")
