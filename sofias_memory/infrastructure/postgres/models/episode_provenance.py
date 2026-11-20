"""EpisodeProvenance ORM model (SM-CTX-001, Contract v1 SS 10).

One-to-one with :class:`~sofias_memory.infrastructure.postgres.models.episode.Episode`
-- ``episode_id`` is both primary key and foreign key. Reuses the existing
``cognitive_memory_origin_kind`` PostgreSQL enum (same five approved values,
handoff 09 SS 14) rather than duplicating it.

``conversation_ref``/``turn_ref`` are opaque external references, never
foreign keys, and never Provider Session IDs. Forget scrubs them; only the
minimal structural provenance (``origin_kind``/``source_system``) survives on
a tombstone. Provenance is not Evidence.
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


class EpisodeProvenance(Base):
    """PostgreSQL source-of-truth row for one Episode's provenance."""

    __tablename__ = "episode_provenance"
    __table_args__ = (
        CheckConstraint(r"source_system ~ '\S'", name="source_system_not_blank"),
        CheckConstraint(
            r"conversation_ref IS NULL OR conversation_ref ~ '\S'",
            name="conversation_ref_not_blank",
        ),
        CheckConstraint(r"turn_ref IS NULL OR turn_ref ~ '\S'", name="turn_ref_not_blank"),
    )

    episode_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("episodes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    origin_kind: Mapped[CognitiveMemoryOriginKind] = mapped_column(
        COGNITIVE_MEMORY_ORIGIN_KIND_ENUM, nullable=False
    )
    source_system: Mapped[str] = mapped_column(Text(), nullable=False)
    conversation_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
    turn_ref: Mapped[str | None] = mapped_column(Text(), nullable=True)
