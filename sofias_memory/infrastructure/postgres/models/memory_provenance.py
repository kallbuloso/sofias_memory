"""MemoryProvenance ORM model (ADR-0016, Feature Contract v0.7.0 SS 7/16.2).

One-to-one with :class:`~sofias_memory.infrastructure.postgres.models.memory_item.MemoryItem`
-- ``memory_id`` is both the primary key and the foreign key, so no
surrogate id and no possibility of more than one provenance row per item.

External references (``conversation_uuid``/``turn_uuid``/``task_uuid``/
``confirmation_ref``/``source_ref``) are opaque values, never cross-database
foreign keys, and never imply Sofias Memory owns Conversation/Turn/Task or
confirmation state.

Deliberately no CHECK constraint enforcing "origin X requires ref Y": those
rules (Feature Contract SS 7.3) apply only to a non-FORGOTTEN item and are
application-enforced (see
:func:`sofias_memory.domain.validate_cognitive_memory_provenance_origin_requirements`).
A DB-level CHECK of that shape would make the Forget-approved scrub --
preserving only ``origin_kind``/``source_system`` with every external ref
set to ``NULL`` -- structurally impossible.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import (
    COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH,
    COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH,
    CognitiveMemoryOriginKind,
)
from sofias_memory.infrastructure.postgres.base import Base


def _cognitive_memory_origin_kind_values(enum_type: type[CognitiveMemoryOriginKind]) -> list[str]:
    return [member.value for member in enum_type]


COGNITIVE_MEMORY_ORIGIN_KIND_ENUM = ENUM(
    CognitiveMemoryOriginKind,
    name="cognitive_memory_origin_kind",
    values_callable=_cognitive_memory_origin_kind_values,
    validate_strings=True,
    create_type=False,
)


class MemoryProvenance(Base):
    """PostgreSQL source-of-truth row for one MemoryItem's cognitive provenance."""

    __tablename__ = "memory_provenance"
    __table_args__ = (
        CheckConstraint(
            "source_system ~ '^[a-z0-9]+(-[a-z0-9]+)*$' "
            f"AND char_length(source_system) <= {COGNITIVE_MEMORY_SOURCE_SYSTEM_MAX_LENGTH}",
            name="source_system_slug",
        ),
        CheckConstraint(
            "confirmation_ref IS NULL OR char_length(confirmation_ref) BETWEEN 1 AND "
            f"{COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH}",
            name="confirmation_ref_length",
        ),
        CheckConstraint(
            "source_ref IS NULL OR char_length(source_ref) BETWEEN 1 AND "
            f"{COGNITIVE_MEMORY_EXTERNAL_REF_MAX_LENGTH}",
            name="source_ref_length",
        ),
    )

    memory_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("memory_items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    origin_kind: Mapped[CognitiveMemoryOriginKind] = mapped_column(
        COGNITIVE_MEMORY_ORIGIN_KIND_ENUM,
        nullable=False,
    )
    source_system: Mapped[str] = mapped_column(Text(), nullable=False)
    conversation_uuid: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=True
    )
    turn_uuid: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    task_uuid: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    confirmation_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
    source_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
