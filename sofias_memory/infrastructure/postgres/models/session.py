"""Session ORM model (ADR-0012, Feature Contract v0.3.0 Sessions SS 4.1)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import SESSION_ID_MAX_LENGTH, SessionStatus
from sofias_memory.infrastructure.postgres.base import Base


def _session_status_values(enum_type: type[SessionStatus]) -> list[str]:
    return [status.value for status in enum_type]


SESSION_STATUS_ENUM = ENUM(
    SessionStatus,
    name="session_status",
    values_callable=_session_status_values,
    validate_strings=True,
    create_type=False,
)


class Session(Base):
    """PostgreSQL source-of-truth row for a first-class durable Session.

    ``key`` is the caller-facing external ``session_id`` (immutable,
    case-sensitive, globally unique); ``id`` is the structural
    ``session_uuid``. Sessions never carry a Dataset FK (ADR-0012: a Session
    may span multiple Datasets) and are never projected to Neo4j.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("length(btrim(key)) > 0", name="key_not_blank"),
        CheckConstraint(f"char_length(key) <= {SESSION_ID_MAX_LENGTH}", name="key_max_length"),
        CheckConstraint(
            "name IS NULL OR char_length(name) <= 120",
            name="name_max_length",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    key: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[SessionStatus] = mapped_column(
        SESSION_STATUS_ENUM,
        nullable=False,
        server_default=SessionStatus.ACTIVE.value,
    )
    metadata_: Mapped[dict[str, object]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=sql_text("'{}'::jsonb"),
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
