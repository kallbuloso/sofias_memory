"""Dataset ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, DateTime, Integer, Text, text
from sqlalchemy.dialects.postgresql import CITEXT, ENUM
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import DatasetStatus
from sofias_memory.infrastructure.postgres.base import Base


def _dataset_status_values(enum_type: type[DatasetStatus]) -> list[str]:
    return [status.value for status in enum_type]


DATASET_STATUS_ENUM = ENUM(
    DatasetStatus,
    name="dataset_status",
    values_callable=_dataset_status_values,
    validate_strings=True,
    create_type=False,
)


class Dataset(Base):
    """PostgreSQL source-of-truth row for a dataset namespace."""

    __tablename__ = "datasets"
    __table_args__ = (
        CheckConstraint("length(btrim(name::text)) > 0", name="name_not_blank"),
        CheckConstraint("char_length(name::text) <= 120", name="name_max_length"),
        CheckConstraint("length(btrim(slug)) > 0", name="slug_not_blank"),
    )

    id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
        default=uuid4,
    )
    name: Mapped[str] = mapped_column(CITEXT(), nullable=False, unique=True)
    slug: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[DatasetStatus] = mapped_column(
        DATASET_STATUS_ENUM,
        nullable=False,
        server_default=DatasetStatus.ACTIVE.value,
    )
    active_generation: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
    )
