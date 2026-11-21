"""ContextCheckpointProvenance ORM model (SM-CTX-002, Contract v1 SS 10,
handoff 10 SS 24/28/46).

One-to-one with ``ContextCheckpoint`` -- ``checkpoint_id`` is both primary key
and foreign key. Reuses the existing ``cognitive_memory_origin_kind`` enum
(same five approved origin kinds).

``conversation_ref``/``turn_ref`` are opaque external references -- never
foreign keys, never a Session owner. Forget scrubs them; only the minimal
structural provenance (``origin_kind``/``source_system``) survives on a
tombstone. Provenance is not Evidence.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Text
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import CognitiveMemoryOriginKind
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.memory_provenance import (
    COGNITIVE_MEMORY_ORIGIN_KIND_ENUM,
)


class ContextCheckpointProvenance(Base):
    """PostgreSQL source-of-truth row for one ContextCheckpoint's provenance."""

    __tablename__ = "context_checkpoint_provenance"
    __table_args__ = (
        CheckConstraint(r"source_system ~ '\S'", name="source_system_not_blank"),
        CheckConstraint(
            r"conversation_ref IS NULL OR conversation_ref ~ '\S'",
            name="conversation_ref_not_blank",
        ),
        CheckConstraint(r"turn_ref IS NULL OR turn_ref ~ '\S'", name="turn_ref_not_blank"),
    )

    checkpoint_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "context_checkpoints.id",
            ondelete="CASCADE",
            name="fk_context_checkpoint_provenance_checkpoint_id",
        ),
        primary_key=True,
    )
    origin_kind: Mapped[CognitiveMemoryOriginKind] = mapped_column(
        COGNITIVE_MEMORY_ORIGIN_KIND_ENUM, nullable=False
    )
    source_system: Mapped[str] = mapped_column(Text(), nullable=False)
    conversation_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
    turn_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
