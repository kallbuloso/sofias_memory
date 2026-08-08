"""Entity mention ORM model."""

from __future__ import annotations

from uuid import UUID, uuid4

from sqlalchemy import REAL, CheckConstraint, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.entity import CONFIDENCE_CHECK_SQL


class EntityMention(Base):
    """Evidence-level mention of an entity within a chunk."""

    __tablename__ = "entity_mentions"
    __table_args__ = (
        CheckConstraint(
            "start_char IS NULL OR start_char >= 0",
            name="start_char_non_negative",
        ),
        CheckConstraint("end_char IS NULL OR end_char >= 0", name="end_char_non_negative"),
        CheckConstraint(
            "start_char IS NULL OR end_char IS NULL OR end_char >= start_char",
            name="char_offsets_valid",
        ),
        CheckConstraint(CONFIDENCE_CHECK_SQL, name="confidence_between_zero_and_one"),
        Index("ix_entity_mentions_entity_id", "entity_id"),
        Index("ix_entity_mentions_chunk_id", "chunk_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    entity_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("entities.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="CASCADE"),
        nullable=False,
    )
    surface_text: Mapped[str] = mapped_column(Text(), nullable=False)
    start_char: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_char: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confidence: Mapped[float] = mapped_column(REAL(), nullable=False)
