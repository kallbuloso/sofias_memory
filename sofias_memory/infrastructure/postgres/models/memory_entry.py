"""Memory entry ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import MemoryEntryType
from sofias_memory.infrastructure.postgres.base import Base


def _memory_entry_type_values(enum_type: type[MemoryEntryType]) -> list[str]:
    return [entry_type.value for entry_type in enum_type]


MEMORY_ENTRY_TYPE_ENUM = ENUM(
    MemoryEntryType,
    name="memory_entry_type",
    values_callable=_memory_entry_type_values,
    validate_strings=True,
    create_type=False,
)


class MemoryEntry(Base):
    """Lightweight persisted memory content."""

    __tablename__ = "memory_entries"
    __table_args__ = (
        Index("ix_memory_entries_dataset_id", "dataset_id"),
        Index("ix_memory_entries_source_id", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    dataset_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("datasets.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[UUID | None] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    entry_type: Mapped[MemoryEntryType] = mapped_column(MEMORY_ENTRY_TYPE_ENUM, nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    metadata_: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
