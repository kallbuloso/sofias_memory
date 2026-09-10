"""Create the Agent <-> Skill explicit association (ADR-0014, SM-803).

Purely additive: ``agent_skills`` only. No other table is touched --
``agent_sessions`` belongs to SM-804, not this migration.

Identity is exactly ``(agent_id, skill_id)`` -- no surrogate UUID PK, no
``updated_at``, no persisted ``status``/``metadata``/redundant revision
integer. Three FKs, matching exactly what the Feature Contract froze:

1. ``agent_id -> agents.id ON DELETE CASCADE`` -- the join row has no
   independent meaning without the Agent.
2. ``skill_id -> skills.id ON DELETE RESTRICT`` -- an association must not
   silently disappear if a Skill were ever physically removed (Skill has no
   public hard delete today; this is defense-in-depth).
3. ``(skill_id, pinned_revision_id) -> skill_revisions(skill_id, id) ON
   DELETE RESTRICT`` -- the same composite-candidate-key pattern
   ``skills.current_revision_id`` already uses (ADR-0013, migration 0014),
   reusing ``skill_revisions``' own ``UNIQUE(skill_id, id)``. This makes two
   failure modes structurally impossible: a pin can never reference a
   revision belonging to a different Skill (cross-Skill pinning), and
   deleting a pinned ``SkillRevision`` is rejected outright rather than
   silently converting a deliberately pinned association into
   follow-current (which ``ON DELETE SET NULL`` would do). No separate
   single-column FK on ``pinned_revision_id`` alone exists.

Unlike migration 0014's composite FK, this one needs no
``DEFERRABLE INITIALLY DEFERRED``/``use_alter``: there is no chicken-egg
problem here (an ``agent_skills`` row always references an already-existing
Skill/SkillRevision, never a not-yet-inserted one in the same transaction),
so all three FKs are declared inline on a single ``CREATE TABLE``.

``pinned_revision_id`` is nullable by design: PostgreSQL's default
``MATCH SIMPLE`` means the composite FK is not enforced at all when
``pinned_revision_id IS NULL`` -- exactly the desired "follow current"
semantics, requiring no special ``MATCH`` clause.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-10 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | Sequence[str] | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_skills",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pinned_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name=op.f("fk_agent_skills_agent_id_agents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id"],
            ["skills.id"],
            name=op.f("fk_agent_skills_skill_id_skills"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["skill_id", "pinned_revision_id"],
            ["skill_revisions.skill_id", "skill_revisions.id"],
            name=op.f("fk_agent_skills_skill_id_pinned_revision_id_skill_revisions"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("agent_id", "skill_id", name=op.f("pk_agent_skills")),
    )


def downgrade() -> None:
    op.drop_table("agent_skills")
