"""Add 'dataset_delete' to the pipeline_type native enum (ADR-0010 D2/D34, SM-515).

No column, no table, no data migration -- exactly the one enum value ADR-0010
D34 requires. ``ALTER TYPE ... ADD VALUE`` cannot safely run inside the same
transaction as a later statement that uses the new value on PostgreSQL, so
this migration runs it in an autocommit block and does nothing else.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-25 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE pipeline_type ADD VALUE IF NOT EXISTS 'dataset_delete'")


def downgrade() -> None:
    # PostgreSQL has no ALTER TYPE ... DROP VALUE. Downgrading a native enum
    # value requires rebuilding the type, which is not attempted here: no
    # migration in this repository ever downgrades past a point where an
    # enum value it added is already in use, and this ADR forbids historical
    # data mutation (ADR-0010 D34).
    raise NotImplementedError(
        "downgrade not supported: PostgreSQL cannot drop a single enum value "
        "(ALTER TYPE ... DROP VALUE does not exist); rebuilding the "
        "pipeline_type enum without 'dataset_delete' would require rewriting "
        "every dependent row and is out of scope for this migration."
    )
