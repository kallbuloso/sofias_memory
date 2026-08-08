"""Entity ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    REAL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
)
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS

CONFIDENCE_CHECK_SQL = "confidence >= 0 AND confidence <= 1"


class Entity(Base):
    """Canonical dataset entity persisted as PostgreSQL source-of-truth knowledge."""

    __tablename__ = "entities"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="generation_non_negative"),
        CheckConstraint(CONFIDENCE_CHECK_SQL, name="confidence_between_zero_and_one"),
        Index(
            "uq_entities_dataset_id_canonical_key_active",
            "dataset_id",
            "canonical_key",
            unique=True,
            postgresql_where=sql_text("is_active IS TRUE"),
        ),
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
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_key: Mapped[str] = mapped_column(Text(), nullable=False)
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    entity_type: Mapped[str] = mapped_column(Text(), nullable=False)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False)
    properties: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(REAL(), nullable=False)
    importance_weight: Mapped[float] = mapped_column(REAL(), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(EMBEDDING_DIMENSIONS),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
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
