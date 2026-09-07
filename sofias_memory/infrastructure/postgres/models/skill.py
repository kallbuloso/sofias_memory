"""Skill ORM model (ADR-0013, Feature Contract v0.4.0 Skills SS 3/4.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import SKILL_NAME_MAX_LENGTH, SkillStatus
from sofias_memory.infrastructure.postgres.base import Base


def _skill_status_values(enum_type: type[SkillStatus]) -> list[str]:
    return [status.value for status in enum_type]


SKILL_STATUS_ENUM = ENUM(
    SkillStatus,
    name="skill_status",
    values_callable=_skill_status_values,
    validate_strings=True,
    create_type=False,
)


class Skill(Base):
    """PostgreSQL source-of-truth row for a first-class durable procedural
    Skill (ADR-0013).

    ``id`` is the structural ``skill_uuid`` -- there is no separate
    ``skill_uuid`` column duplicating it (mirrors ``Session.id`` /
    ``session_uuid``, ADR-0012). ``name`` is the portable, immutable,
    globally unique logical identity. Skills never carry a
    Dataset/Session/Agent FK and are never projected to Neo4j.

    ``current_revision_id`` is enforced NOT NULL from the first committed
    row: the composite ``DEFERRABLE INITIALLY DEFERRED`` foreign key below,
    targeting ``SkillRevision``'s ``UNIQUE(skill_id, id)`` candidate key,
    both (a) guarantees at the database level that a Skill's current
    revision always belongs to that exact Skill, and (b) defers that check
    to transaction commit, so the initial aggregate (insert ``Skill`` with
    ``current_revision_id`` already pointing at a not-yet-inserted
    ``SkillRevision`` row, then insert that row, then commit) is a single
    atomic transaction with no nullable-then-update step and no window
    where a committed Skill can ever be observed without a revision.
    """

    __tablename__ = "skills"
    __table_args__ = (
        CheckConstraint(
            f"char_length(name) <= {SKILL_NAME_MAX_LENGTH}",
            name="name_max_length",
        ),
        CheckConstraint(
            "name ~ '^[a-z0-9]+(-[a-z0-9]+)*$'",
            name="name_portable_charset",
        ),
        ForeignKeyConstraint(
            ["id", "current_revision_id"],
            ["skill_revisions.skill_id", "skill_revisions.id"],
            name="fk_skills_current_revision_skill_revisions",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    status: Mapped[SkillStatus] = mapped_column(
        SKILL_STATUS_ENUM,
        nullable=False,
        server_default=SkillStatus.ACTIVE.value,
    )
    current_revision_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
