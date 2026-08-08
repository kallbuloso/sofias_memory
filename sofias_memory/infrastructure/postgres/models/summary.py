"""Summary ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Text
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import SummaryTargetType
from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.chunk import EMBEDDING_DIMENSIONS


def _summary_target_type_values(enum_type: type[SummaryTargetType]) -> list[str]:
    return [target_type.value for target_type in enum_type]


SUMMARY_TARGET_TYPE_ENUM = ENUM(
    SummaryTargetType,
    name="summary_target_type",
    values_callable=_summary_target_type_values,
    validate_strings=True,
    create_type=False,
)


class Summary(Base):
    """Generated retrieval summary for a dataset-scoped target."""

    __tablename__ = "summaries"
    __table_args__ = (
        CheckConstraint("generation >= 0", name="generation_non_negative"),
        Index(
            "ix_summaries_dataset_id_generation_is_active", "dataset_id", "generation", "is_active"
        ),
        Index(
            "ix_summaries_embedding_halfvec_hnsw",
            sql_text("(embedding::halfvec(3072)) halfvec_cosine_ops"),
            postgresql_using="hnsw",
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
    target_type: Mapped[SummaryTargetType] = mapped_column(SUMMARY_TARGET_TYPE_ENUM, nullable=False)
    target_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
