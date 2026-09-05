"""Add the missing authoritative trim invariant for session_entries.external_id.

Migration 0012 (already committed/published -- not edited here) created
``external_id`` with non-blank and max-length checks plus the
``(session_id, external_id)`` partial unique index, but no constraint
actually enforcing that a stored value is already trimmed. This migration
adds exactly that one missing invariant. Purely additive: no data mutation,
no backfill, no normalization of any pre-existing row. If any existing row
already violates the invariant, this migration fails loudly rather than
silently rewriting historical data.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-05 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT_NAME = "ck_session_entries_external_id_trimmed"
CONSTRAINT_CONDITION = "external_id IS NULL OR external_id = btrim(external_id)"


def upgrade() -> None:
    op.create_check_constraint(op.f(CONSTRAINT_NAME), "session_entries", CONSTRAINT_CONDITION)


def downgrade() -> None:
    op.drop_constraint(op.f(CONSTRAINT_NAME), "session_entries", type_="check")
