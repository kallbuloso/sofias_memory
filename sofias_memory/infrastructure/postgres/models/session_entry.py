"""SessionEntry ORM model (ADR-0012, Feature Contract v0.3.0 Sessions SS 4.2)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import SESSION_ID_MAX_LENGTH
from sofias_memory.infrastructure.postgres.base import Base


class SessionEntry(Base):
    """Append-only durable contextual history record for a Session.

    ``role`` is open-ended ``TEXT`` (not a PostgreSQL enum): it is contextual
    metadata only and is never mapped to a privileged LLM provider role. This
    model is persistence-only in SM-601 -- no public API exists yet.

    ``external_id`` is an optional, immutable, caller-supplied correlation
    identity scoped to one Session (e.g. for future retry-safe append), not
    a Session-wide external key like ``Session.key`` -- it reuses
    ``SESSION_ID_MAX_LENGTH`` only because the two happen to share the same
    255-character bound, not because they are the same concept.
    """

    __tablename__ = "session_entries"
    __table_args__ = (
        Index("ix_session_entries_session_id_created_at_id", "session_id", "created_at", "id"),
        Index(
            "uq_session_entries_session_id_external_id",
            "session_id",
            "external_id",
            unique=True,
            postgresql_where=sql_text("external_id IS NOT NULL"),
        ),
        CheckConstraint(
            "external_id IS NULL OR length(btrim(external_id)) > 0",
            name="external_id_not_blank",
        ),
        CheckConstraint(
            f"external_id IS NULL OR char_length(external_id) <= {SESSION_ID_MAX_LENGTH}",
            name="external_id_max_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    session_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    external_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    role: Mapped[str] = mapped_column(Text(), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    metadata_: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
