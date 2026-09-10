"""AgentSkill ORM model (ADR-0014, v0.5.0 Agent Management Feature Contract SS 11)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base


class AgentSkill(Base):
    """PostgreSQL source-of-truth row for the explicit Agent <-> Skill
    association (ADR-0014).

    Identity is exactly ``(agent_id, skill_id)`` -- no surrogate UUID PK, no
    ``updated_at``, no persisted ``status``/``metadata``. ``pinned_revision_id``
    is nullable: ``NULL`` means the association follows ``Skill.current_revision_id``
    live; a value means the association is frozen to that exact
    ``SkillRevision`` until explicitly re-pinned or un-pinned.

    The composite FK below reuses ``SkillRevision``'s own
    ``UNIQUE(skill_id, id)`` candidate key (the same one
    ``Skill.current_revision_id`` targets) -- this makes cross-Skill pinning
    structurally impossible and rejects deletion of a pinned revision
    outright (``RESTRICT``, never ``SET NULL``), so a pinned association can
    never be silently converted into follow-current behavior.
    """

    __tablename__ = "agent_skills"
    __table_args__ = (
        ForeignKeyConstraint(
            ["skill_id", "pinned_revision_id"],
            ["skill_revisions.skill_id", "skill_revisions.id"],
            name="fk_agent_skills_skill_id_pinned_revision_id_skill_revisions",
            ondelete="RESTRICT",
        ),
    )

    agent_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skill_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("skills.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    pinned_revision_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
