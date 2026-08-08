"""Source ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.domain import SourceKind, SourceStatus
from sofias_memory.infrastructure.postgres.base import Base


def _source_kind_values(enum_type: type[SourceKind]) -> list[str]:
    return [kind.value for kind in enum_type]


def _source_status_values(enum_type: type[SourceStatus]) -> list[str]:
    return [status.value for status in enum_type]


SOURCE_KIND_ENUM = ENUM(
    SourceKind,
    name="source_kind",
    values_callable=_source_kind_values,
    validate_strings=True,
    create_type=False,
)
SOURCE_STATUS_ENUM = ENUM(
    SourceStatus,
    name="source_status",
    values_callable=_source_status_values,
    validate_strings=True,
    create_type=False,
)

SHA256_HEX_PATTERN = "'^[0-9a-fA-F]{64}$'"


class Source(Base):
    """Original input registered for ingestion or later cognification."""

    __tablename__ = "sources"
    __table_args__ = (
        CheckConstraint(f"content_sha256 ~ {SHA256_HEX_PATTERN}", name="content_sha256_hex"),
        CheckConstraint(
            f"normalized_sha256 IS NULL OR normalized_sha256 ~ {SHA256_HEX_PATTERN}",
            name="normalized_sha256_hex",
        ),
        UniqueConstraint(
            "dataset_id",
            "content_sha256",
            "version",
            name="uq_sources_dataset_id_content_sha256_version",
        ),
        Index("ix_sources_metadata", "metadata", postgresql_using="gin"),
        Index("ix_sources_status", "status"),
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
    kind: Mapped[SourceKind] = mapped_column(SOURCE_KIND_ENUM, nullable=False)
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    mime_type: Mapped[str] = mapped_column(Text(), nullable=False)
    original_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    storage_uri: Mapped[str | None] = mapped_column(Text(), nullable=True)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    normalized_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    metadata_: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)
    status: Mapped[SourceStatus] = mapped_column(SOURCE_STATUS_ENUM, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
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
