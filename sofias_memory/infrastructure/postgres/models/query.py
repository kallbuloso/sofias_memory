"""Query audit ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base


class Query(Base):
    """Persisted query audit metadata and optional content."""

    __tablename__ = "queries"

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    query_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    dataset_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PostgreSQLUUID(as_uuid=True)),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(Text(), nullable=False)
    answer: Mapped[str | None] = mapped_column(Text(), nullable=True)
    references: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    timings: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    model: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
