"""Relation evidence ORM model."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import REAL, CheckConstraint, ForeignKey, Index, Text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.entity import CONFIDENCE_CHECK_SQL


class RelationEvidence(Base):
    """Chunk evidence supporting a relation."""

    __tablename__ = "relation_evidence"
    __table_args__ = (
        CheckConstraint(CONFIDENCE_CHECK_SQL, name="confidence_between_zero_and_one"),
        Index("ix_relation_evidence_chunk_id", "chunk_id"),
    )

    relation_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("relations.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chunk_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("chunks.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    quote: Mapped[str] = mapped_column(Text(), nullable=False)
    confidence: Mapped[float] = mapped_column(REAL(), nullable=False)
