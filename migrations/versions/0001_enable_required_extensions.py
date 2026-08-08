"""Enable required PostgreSQL extensions.

Revision ID: 0001
Revises:
Create Date: 2026-08-08 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")


def downgrade() -> None:
    """Leave shared database capabilities installed.

    The upgrade is idempotent and cannot prove whether these extensions already
    existed before Sofias Memory ran this migration. Removing them automatically
    could break managed database provisioning or later objects owned outside this
    migration chain. Later downgrades should remove Sofias Memory schema objects,
    but not destroy shared database capabilities blindly.
    """
