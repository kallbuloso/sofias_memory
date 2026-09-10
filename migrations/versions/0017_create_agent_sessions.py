"""Create the Agent <-> Session explicit association (ADR-0014, SM-804).

Purely additive: ``agent_sessions`` only. No other table is touched.

Identity is exactly ``(agent_id, session_id)`` -- no surrogate UUID PK, no
``updated_at``, no persisted ``status``/``metadata``/``role``. Exactly two
FKs, matching exactly what the Feature Contract froze:

1. ``agent_id -> agents.id ON DELETE CASCADE`` -- the join row has no
   independent meaning without the Agent.
2. ``session_id -> sessions.id ON DELETE CASCADE`` -- unlike
   ``agent_skills.skill_id`` (RESTRICT), both sides here are CASCADE. This
   does NOT make the association provenance: ``agent_sessions`` records a
   *current explicit management association* only (ADR-0014), never
   historical participation, Session ownership, or operation attribution.
   ``DELETE`` on this table is permitted and idempotent precisely because
   it carries no audit obligation -- CASCADE from either parent is simply
   structural join-row cleanup, the same as any other pure association
   table, not "forgetting history" (there is no history stored here to
   forget).

No third FK exists: ``agent_sessions`` never references ``queries``,
``pipeline_runs``, ``session_entries``, ``datasets``, ``skills``, or
``skill_revisions``. ``Query``/``PipelineRun`` Session provenance (ADR-0012)
is untouched by this migration -- no ``agent_id`` column is added to either
table, by design: a Session with more than one associated Agent makes
per-operation Agent attribution structurally undeterminable, and this
migration does not attempt to solve that (Feature Contract SS 35, 40-42).

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-11 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_agent_sessions_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["sessions.id"],
            name=op.f("fk_agent_sessions_session_id_sessions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("agent_id", "session_id", name=op.f("pk_agent_sessions")),
    )


def downgrade() -> None:
    op.drop_table("agent_sessions")
