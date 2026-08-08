"""Chunk ORM model."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy import (
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from sofias_memory.infrastructure.postgres.base import Base
from sofias_memory.infrastructure.postgres.models.source import SHA256_HEX_PATTERN

EMBEDDING_DIMENSIONS = 3072


class Chunk(Base):
    """Stable chunk text, lexical data, and authoritative embedding storage."""

    __tablename__ = "chunks"
    __table_args__ = (
        CheckConstraint(f"content_sha256 ~ {SHA256_HEX_PATTERN}", name="content_sha256_hex"),
        CheckConstraint("generation >= 0", name="generation_non_negative"),
        CheckConstraint("ordinal >= 0", name="ordinal_non_negative"),
        CheckConstraint("token_count >= 0", name="token_count_non_negative"),
        CheckConstraint(
            "start_char >= 0 AND end_char >= start_char",
            name="char_offsets_valid",
        ),
        UniqueConstraint(
            "document_id",
            "generation",
            "ordinal",
            name="uq_chunks_document_id_generation_ordinal",
        ),
        Index("ix_chunks_lexical", "lexical", postgresql_using="gin"),
        Index("ix_chunks_dataset_id_is_active", "dataset_id", "is_active"),
        Index("ix_chunks_source_id_is_active", "source_id", "is_active"),
        Index(
            "ix_chunks_embedding_halfvec_hnsw",
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
    document_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("sources.id", ondelete="RESTRICT"),
        nullable=False,
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text(), nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)
    section_path: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False)
    metadata_: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)
    lexical: Mapped[str] = mapped_column(TSVECTOR(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_text("now()"),
    )
